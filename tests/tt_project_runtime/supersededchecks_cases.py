# Checks reaping part 2 tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file (and the
# detach case file's checks-run helpers). Superseded check sets, the coordinator's stop of a waiting
# task's checks, and no remote check on a host off the spill list. Fake hosts only: no ssh.
from __future__ import annotations


def _coord_run(p, name, why="checks run under an old set"):
    """A coordinator run (its run dir); with `why`, a `ttp audit why` recorded in it. Its id."""
    from ttp import coordfix
    d = p.runs / name
    d.mkdir(parents=True, exist_ok=True)
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (None, "coordinator", "fake", time.time(), "running", str(d)))
    if why:
        (d / coordfix.INTENT).write_text(json.dumps({"ts": time.time(), "why": why, "serves": "o1", "undo": "rerun"}))
    return p.db.one("SELECT id FROM runs WHERE dir=?", (str(d),))["id"]


def test_coordinator_stops_a_waiting_tasks_checks_only_with_an_audit_why(env, tmp_path):
    """A coordinator run with a recorded `ttp audit why` may stop the detached checks of a waiting task:
    they end, the task's checks.rc says dropped (it wakes and reruns them) and the event names the why.
    Without the why, from a worker run, or for a running task, the old refusal stands."""
    from ttp import jobstop
    p = make(env)
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="waiting")
    worker = _add_run(p, tid, "worker-run")
    bare = _coord_run(p, "coord-bare", why="")
    coord = _coord_run(p, "coord-why")
    d, _, proc = _json_checks_run(p, tid, "checks-wait")
    try:
        rc = d / "checks.rc"
        for run in (bare, worker):
            ok, why = jobstop.may_stop(p, rc, None, str(run))
            assert not ok and "refused" in why, why
        p.db.update_task(tid, status="running")
        assert not jobstop.may_stop(p, rc, None, str(coord))[0], "a running task's checks stay"
        p.db.update_task(tid, status="waiting")
        assert not jobstop.may_stop(p, d / "job.rc", None, str(coord))[0], "checks only, not other jobs"
        code, text, out = jobstop.stop_checks(p, d, None, str(coord))
        assert code == 0 and out and proc.wait(timeout=10), text
        assert "checks run under an old set" in text and "serves o1" in text, text
        assert rc.read_text() == "1\n" and "checks run under an old set" in (d / "checks.dropped").read_text()
        assert "run `ttp checks --detach` again" in (d / "checks.out").read_text()
    finally:
        _stop_checks_group(proc)


def test_checks_of_a_superseded_check_set_are_stopped_and_dropped(env, tmp_path):
    """An open task's detached checks that recorded another check set than delivery.push_checks now holds
    are ended and dropped (the task reruns them); those of the current set, of named commands (no set)
    and of an ended task are left to the other rules."""
    from ttp import jobstop, push
    p = make(env)
    p.set_config("delivery.push_checks", ["pytest -q tests/smoke"])
    current = push.checks_set_id(p.config().get("delivery") or {})
    old = push.checks_set_id({"push_checks": ["pytest -q tests"]})
    assert old != current and current == push.checks_set_id({"push_checks": ["pytest -q tests/smoke"]})
    tid = p.db.add_task("t", "s", kind="code", tier="light", origin="user")
    p.db.update_task(tid, status="waiting")
    runs = {}
    try:
        for name, mark in (("stale", old), ("fresh", current), ("named", None)):
            d, _, proc = _json_checks_run(p, tid, f"checks-{name}")
            info = json.loads((d / "checks.pid").read_text())
            if mark:
                (d / "checks.pid").write_text(json.dumps({**info, "checks_set": mark}))
            runs[name] = (d, proc)
        (out,) = jobstop.sweep_superseded_checks(p)
        d, proc = runs["stale"]
        assert out["task"] == tid and proc.wait(timeout=10) and "superseded check set" in out["text"]
        assert (d / "checks.rc").read_text() == "1\n" and "superseded" in (d / "checks.dropped").read_text()
        assert all(runs[n][1].poll() is None for n in ("fresh", "named"))
        assert jobstop.sweep_superseded_checks(p) == []
    finally:
        for _, proc in runs.values():
            _stop_checks_group(proc)


def test_a_detached_checks_run_records_its_check_set_only_for_configured_checks(env):
    """checks.pid's checks_set: the configured checks' id; none for named commands or harness checks."""
    import argparse
    from ttp import cli, push
    p = make(env)
    p.set_config("delivery.push_checks", ["make smoke"])
    want = push.checks_set_id(p.config().get("delivery") or {})
    assert cli._checks_set(argparse.Namespace(cmd=[], purpose=None), str(p.base)) == {"checks_set": want}
    assert cli._checks_set(argparse.Namespace(cmd=["make t"], purpose=None), "/x") == {}
    assert cli._checks_set(argparse.Namespace(cmd=[], purpose="harness"), "/x") == {}


def test_no_remote_check_starts_on_a_host_taken_off_the_spill_list(env, tmp_path, monkeypatch):
    """A spilled check carries its project (--spill); remotecheck refuses the host with an environment
    fault (75, so the run reruns elsewhere) once the project's list no longer has it. A pinned host
    (no spill) is not asked about the list."""
    from ttp import checkspill, push, remotecheck
    p = make(env)
    monkeypatch.setattr(checkspill, "account_hosts", lambda: [])
    p.set_config(f"delivery.{checkspill.KEY}", ["spill-a", "spill-b"])
    assert checkspill.off_list(str(p.base), "spill-b") == ""
    assert checkspill.off_list(str(tmp_path / "no-project"), "spill-z") == ""
    (spilled,) = push.spill_to(push.check_list(["make t"]), "spill-b", p.config())
    spilled.spill_project = str(p.base)
    argv = push.check_argv(spilled)
    assert argv[argv.index("--spill") + 1] == str(p.base)
    (pinned,) = push.with_host(push.check_list(["make t"]), "box1")
    assert "--spill" not in push.check_argv(pinned)
    p.set_config(f"delivery.{checkspill.KEY}", ["spill-a"])
    assert "no longer one of the project's spill hosts" in checkspill.off_list(str(p.base), "spill-b")
    monkeypatch.setattr(remotecheck, "_git", lambda *a: pytest.fail("checked out a commit for a host off the list"))
    assert remotecheck.main(["--host", "spill-b", "--spill", str(p.base), "--", "make t"]) == remotecheck.FAULT_RC
