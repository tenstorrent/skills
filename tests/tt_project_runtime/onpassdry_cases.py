# on_pass_cmd dry-run (onpassdry.py) tests of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there). Add new on_pass_cmd dry-run tests here, not there.
from __future__ import annotations

from ttp.db import load_result


_ON_PASS = {"status": "done", "summary": "job passed"}


def _hand_off_waiting(env, p, d, monkeypatch, cmd, title="waits on its job", probe="false", on_pass=_ON_PASS):
    """A fake worker run of a new task that hands off `waiting` with on_pass_cmd `cmd` (and `on_pass`,
    left out when None); the task's row."""
    monkeypatch.setenv("TTP_FAKE_SESSION", "s1")
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
        "status": "waiting", "summary": "job runs", "waiting_for": "its job", "retry_when": probe,
        "retry_after_s": 600, "on_pass_cmd": cmd, **({"on_pass": on_pass} if on_pass is not None else {})}))
    tid = p.db.add_task(title, "run the job", kind="work", tier="light", origin="user")
    d.dispatch()
    _finish_runs(p, d)
    return p.db.task(tid)


def _last_run(p, tid):
    return p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))


def test_an_on_pass_cmd_that_cannot_run_as_written_hands_the_run_back_to_its_session_once(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    t = _hand_off_waiting(env, p, d, monkeypatch, "bash no_such_script_xyz.sh")
    res = load_result(t["result"])
    assert t["status"] == "queued" and (t["not_before"] or 0) <= time.time() + 1, dict(t)
    assert res[onpassdry.KEY]["session"] == "s1" and "waiting_since" not in res
    assert "no_such_script_xyz.sh: no such file" in res["woke"] and "cannot run as written" in res["woke"]
    first = _last_run(p, t["id"])
    got = json.loads((pathlib.Path(first["dir"]) / onpassdry.FILE).read_text())
    assert got["verdict"] == onpassdry.CRASH and got["rc"] is None and got["session"] == "s1"
    back = d._resumable(t, "fake")
    assert back["cause"] == "dry_run" and back["session"] == "s1" and back["run"] == first["id"]
    # Its next run continues the same session and is told why its hand-off was not taken.
    d.dispatch()
    run = _last_run(p, t["id"])
    assert run["id"] != first["id"]
    run_dir = pathlib.Path(run["dir"])
    assert json.loads((run_dir / "run.json").read_text())["argv"][-2:] == ["--resume", "s1"]
    prompt = (run_dir / "prompt.md").read_text()
    assert "was not taken" in prompt and "no_such_script_xyz.sh" in prompt and "do not start them again" in prompt
    _finish_runs(p, d)
    # The handed-back run hands off the same: once per hand-off, so it stands now with a warning.
    t = p.db.task(t["id"])
    res = load_result(t["result"])
    assert t["status"] == "queued" and res.get("waiting_since"), dict(t)
    assert onpassdry.KEY not in res and res[onpassdry.NOTE_KEY].startswith("WARNING: on_pass_cmd cannot run as written")
    assert res["summary"].startswith("WARNING: on_pass_cmd cannot run as written"), res["summary"]
    assert "WARNING: on_pass_cmd cannot run as written" in (run_dir / "progress.md").read_text()
    assert d._resumable(t, "fake") is None


def test_a_failed_dry_run_with_no_session_to_continue_keeps_the_hand_off_with_a_warning(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    monkeypatch.delenv("TTP_FAKE_SESSIONS", raising=False)
    d = Daemon(p.base)
    t = _hand_off_waiting(env, p, d, monkeypatch, "no_such_prog_xyz --publish")
    res = load_result(t["result"])
    assert t["status"] == "queued" and res.get("waiting_since") and onpassdry.KEY not in res
    assert res[onpassdry.NOTE_KEY].startswith("WARNING: on_pass_cmd cannot run as written")
    assert "no_such_prog_xyz: command not found" in res[onpassdry.NOTE_KEY]
    assert res["summary"].startswith("WARNING:") and "job runs" in res["summary"]
    assert (t["not_before"] or 0) > time.time() + 300, "the hand-off's retry_after_s stands"


_WRITES_DONE = 'printf \'{"status":"done","summary":"published"}\' > "$TTP_RESULT"'


def test_the_dry_run_never_runs_the_on_pass_cmd_even_one_that_checks_its_variable(env, monkeypatch, tmp_path):
    # An on_pass_cmd is often a publish step: run at hand-off, it published a head whose checks then failed.
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    marker = tmp_path / "published"
    for i, cmd in enumerate((f"touch {marker}; {_WRITES_DONE}",
                             f'[ "$TTP_ON_PASS_DRY_RUN" = 1 ] && exit 0; touch {marker}; {_WRITES_DONE}')):
        t = _hand_off_waiting(env, p, d, monkeypatch, cmd, title=f"case {i}")
        res = load_result(t["result"])
        assert not marker.exists(), f"the dry run ran {cmd!r}"
        assert onpassdry.KEY not in res and onpassdry.NOTE_KEY not in res and res.get("waiting_since")
        assert res["summary"] == "job runs"
        got = json.loads((pathlib.Path(_last_run(p, t["id"])["dir"]) / onpassdry.FILE).read_text())
        assert got["verdict"] == "ok" and got["rc"] is None and got["cmd"] == cmd, got


def test_an_on_pass_cmd_runs_only_after_its_checks_pass_never_at_hand_off_or_after_they_fail(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import onpassdry, daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    marker = tmp_path / "published"
    cmd = f"touch {marker}; {_WRITES_DONE}"
    for rc in ("1", "killed", "0"):
        # Handed off while its checks run (no checks.rc yet): the dry run reads the command, runs nothing.
        tid, _ = _checks_wait_task(p, tmp_path, DONE_ON_PASS, rc=None, on_pass_cmd=cmd)
        run_dir = tmp_path / f"run-{tid}"
        from ttp import cli, runner
        (run_dir / cli.CHECKS_PID).write_text(json.dumps({"pid": os.getpid(),   # still running
                                                          "started": runner.proc_start(os.getpid())}))
        d._dry_run_on_pass(dict(p.db.one("SELECT * FROM runs WHERE task=?", (tid,))), run_dir, "")
        assert json.loads((run_dir / onpassdry.FILE).read_text())["verdict"] == "ok"
        assert not marker.exists(), "the dry run ran the on_pass_cmd at hand-off"
        _settle_on_pass(d, tid)
        assert not marker.exists(), "it ran while its checks ran"
        assert "woke" not in load_result(p.db.task(tid)["result"]), "its wait ended while its checks ran"
        (run_dir / cli.CHECKS_PID).unlink()
        (run_dir / "checks.rc").write_text(f"{rc}\n")
        if rc == "0":
            (run_dir / "checks.json").write_text(json.dumps({**json.loads((run_dir / "checks.json").read_text()),
                                                             "passed": True}))
        _settle_on_pass(d, tid)
        task = p.db.task(tid)
        if rc != "0":
            assert not marker.exists(), f"it ran after its checks ended {rc}"
            assert task["status"] != "done" and f"its checks failed" in load_result(task["result"])["woke"]
        else:
            assert marker.exists(), "it did not run once its checks passed"
            assert task["status"] == "done" and load_result(task["result"])["summary"] == "published"


def test_an_on_pass_cmd_never_runs_when_its_checks_left_no_record_or_none_for_its_head(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    marker = tmp_path / "published"
    cmd = f"touch {marker}; {_WRITES_DONE}"
    # No checks.pid and no checks.rc: `ttp checks --result` passes ("no detached checks recorded").
    tid, _ = _checks_wait_task(p, tmp_path, DONE_ON_PASS, rc=None, on_pass_cmd=cmd)
    _settle_on_pass(d, tid)
    task = p.db.task(tid)
    assert not marker.exists() and task["status"] == "queued"
    assert "its checks left no passing result" in load_result(task["result"])["woke"]
    # Exit 0 with no on_pass: it still runs only for a pass on the head still checked out.
    tid, path = _checks_wait_task(p, tmp_path, rc="0", on_pass_cmd=cmd)
    (path / "fix.txt").write_text("changed after its checks\n")
    _settle_on_pass(d, tid)
    task = p.db.task(tid)
    assert not marker.exists() and task["status"] == "queued"
    assert "its on_pass was not applied" in load_result(task["result"])["woke"]
    (path / "fix.txt").write_text("fixed\n")
    p.db.update_task(tid, status="queued", not_before=time.time() + 3600,
                     result=json.dumps({k: v for k, v in load_result(task["result"]).items() if k != "woke"}))
    _settle_on_pass(d, tid)
    assert marker.exists() and p.db.task(tid)["status"] == "done"


def test_a_dry_run_check_that_times_out_is_inconclusive_and_the_hand_off_stands(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    d.cfg["waiting"]["on_pass_dry_run_timeout_s"] = 1e-9
    t = _hand_off_waiting(env, p, d, monkeypatch, "bash no_such_script_xyz.sh")
    res = load_result(t["result"])
    assert res[onpassdry.NOTE_KEY].startswith("dry run inconclusive (its check timed out"), res
    assert onpassdry.KEY not in res and res.get("waiting_since") and res["summary"] == "job runs"


def test_the_dry_run_can_be_switched_off(env, monkeypatch):
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    d.cfg["waiting"]["on_pass_dry_run"] = False
    t = _hand_off_waiting(env, p, d, monkeypatch, "bash no_such_script_xyz.sh")
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
    assert res[onpassdry.KEY]["no_result"] and "writes a hand-off to $TTP_RESULT" in res["woke"]
    got = json.loads((pathlib.Path(_last_run(p, t["id"])["dir"]) / onpassdry.FILE).read_text())
    assert got["verdict"] == onpassdry.NO_RESULT and got["rc"] is None and onpassdry.said(got) == "checked, not run"
    assert d._resumable(t, "fake")["cause"] == "dry_run"
    # The handed-back run hands off the same: it stands now, with the reason in its summary.
    d.dispatch()
    _finish_runs(p, d)
    res = load_result(p.db.task(t["id"])["result"])
    assert res.get("waiting_since") and onpassdry.KEY not in res
    assert res["summary"].startswith("WARNING: on_pass_cmd dry run: nothing in it"), res["summary"]


def test_a_checks_result_wait_counts_its_on_pass_fallback_only_when_that_on_pass_is_valid(env, monkeypatch, tmp_path):
    # At probe pass (_checks_wait) on_pass is the fallback of an exit 0 without $TTP_RESULT only when the
    # hand-off carries one on_pass_problem accepts: the dry run must count it on the same terms.
    p = make(env)
    from ttp import onpassdry
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    probe = f"ttp checks --result {tmp_path / 'checks'}"
    cases = [(None, True), ({"status": "waiting", "summary": "x"}, True), ({"status": "done"}, True),
             ({"status": "done", "summary": "ok", "retry_when": "true"}, True), (_ON_PASS, False)]
    for i, (on_pass, handed_back) in enumerate(cases):
        t = _hand_off_waiting(env, p, d, monkeypatch, "echo checked", title=f"case {i}", probe=probe, on_pass=on_pass)
        res = load_result(t["result"])
        got = json.loads((pathlib.Path(_last_run(p, t["id"])["dir"]) / onpassdry.FILE).read_text())
        if handed_back:
            assert got["verdict"] == onpassdry.NO_RESULT and res[onpassdry.KEY]["no_result"], (on_pass, got)
            assert "writes a hand-off to $TTP_RESULT" in res["woke"]
        else:
            assert got["verdict"] == "ok" and onpassdry.KEY not in res and res.get("waiting_since"), (on_pass, got)


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
    assert got["verdict"] == "ok" and got["rc"] is None and "shell syntax" in got["checked"]
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


def test_the_dry_run_check_reads_programs_scripts_and_variables_without_running_them(env, tmp_path):
    from ttp import onpassdry
    e = {k: v for k, v in os.environ.items() if k not in ("TTP_RUN_DIR", "TTP_RUN_ID")}
    e["HOME"] = str(tmp_path)
    marker = tmp_path / "published"

    def verdict(cmd, fallback=False):
        got = onpassdry.check(cmd, tmp_path, e, fallback, 30)
        assert got["rc"] is None and not marker.exists(), (cmd, got)
        return got["verdict"]

    def script(name, text, mode=0o755):
        (tmp_path / name).write_text(text)
        (tmp_path / name).chmod(mode)
    script("good.sh", f"#!/bin/sh\ntouch {marker}\n{_WRITES_DONE}\n")
    script("good.py", f"import os\nopen({str(marker)!r}, 'w')\nopen(os.environ['TTP_RESULT'], 'w').write('{{}}')\n")
    script("bad.sh", "#!/bin/sh\nif then\n")
    script("bad.py", "def f(:\n")
    script("noexec.sh", f"#!/bin/sh\n{_WRITES_DONE}\n", 0o644)
    script("plain.sh", f"#!/bin/sh\ntouch {marker}\n")
    for cmd in (f"touch {marker}; {_WRITES_DONE}", "bash good.sh", "./good.sh", "~/good.sh", "python3 good.py",
                f"python3 -u {tmp_path}/good.py", "timeout -s KILL 60 env A=1 ./good.sh 2>&1 | tee log",
                f'[ -n "$TTP_ON_PASS_DRY_RUN" ] || touch {marker}; ./good.sh', "true --script=good.py",
                "python3 -m some_publisher", "x=$(cat good.sh); true", "cd sub && ./missing.sh"):
        assert verdict(cmd) == "ok", cmd
    # Cannot run as written: a parse error, a missing or non-executable program or script, a script
    # that does not parse, a run-only variable (unset once its probe passes), a missing input.
    for cmd in ("if true; then touch x", "bash missing.sh", "./missing.sh", "no_such_prog_xyz", "python3 bad.py",
                "./bad.sh", "bash bad.sh", ". ./bad.sh", "./noexec.sh", "A=1 ./missing.sh > out 2>&1",
                f"echo $TTP_RUN_DIR; {_WRITES_DONE}", f"echo ${{TTP_RUN_ID}}; {_WRITES_DONE}",
                'cat < missing.json > "$TTP_RESULT"'):
        assert verdict(cmd) == onpassdry.CRASH, cmd
    # Nothing in it writes a hand-off; a `ttp checks --result` wait's valid on_pass is its fallback.
    for cmd in ("true", "bash plain.sh", "python3 -c 'print(1)'", "true " + "x" * 5000):
        assert verdict(cmd) == onpassdry.NO_RESULT, cmd
    assert verdict("true", fallback=True) == "ok"
    # A check that runs out of time is inconclusive, never a crash.
    assert onpassdry.check("bash missing.sh", tmp_path, e, False, 0)["verdict"] == "inconclusive"


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
