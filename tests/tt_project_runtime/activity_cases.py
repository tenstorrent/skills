# Machine activity tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new `ttp machines activity` and activity log tests here.
from __future__ import annotations


def _run_env(env, p, **kw):
    return dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base),
                TTP_TASK="7", TTP_RUN_ID="9", **kw)


def _snapshot(root: pathlib.Path) -> dict:
    """Every file under root with its size, mtime and content: a read-only reader changes none of it.
    SQLite's -shm index is left out: any reader of a WAL database takes its read marks there."""
    return {str(f): (f.stat().st_size, f.stat().st_mtime_ns, f.read_bytes())
            for f in sorted(root.rglob("*")) if f.is_file() and not f.name.endswith("-shm")}


def _span(state, resource, start, end=None, host="testhost", task="1", **kw):
    from ttp import activity
    aid = activity.new_id()
    activity.append(state, {"ev": "take", "id": aid, "project": "x", "task": task, "run": "2", "host": host,
                            "resource": resource, "mode": "lock", "cmd": f"cmd on {resource}", "start": start, **kw})
    if end is not None:
        activity.append(state, {"ev": "release", "id": aid, "end": end, "rc": 0})
    return aid


def test_ttp_lock_logs_take_and_release_with_a_capped_command_head_and_the_disruptive_flag(env):
    from ttp import activity
    p = make(env)
    long_arg = "x" * 300
    cmd = [sys.executable, str(TTP), "lock", "board", "--disruptive", "--", sys.executable, "-c",
           f"import sys; sys.exit(3)  # {long_arg}"]
    assert subprocess.run(cmd, env=_run_env(env, p), timeout=60).returncode == 3
    cmd = [sys.executable, str(TTP), "lock", "board", "--", "true"]
    assert subprocess.run(cmd, env=_run_env(env, p), timeout=60).returncode == 0
    recs = activity.entries(p.state)
    takes = [r for r in recs if r["ev"] == "take"]
    assert [r["resource"] for r in takes] == ["board", "board"]
    assert takes[0]["disruptive"] is True and "disruptive" not in takes[1]
    assert takes[0]["task"] == "7" and takes[0]["run"] == "9" and takes[0]["host"] == "testhost"
    assert takes[0]["mode"] == "lock" and len(takes[0]["cmd"]) == activity.HEAD
    spans = activity.spans(recs)
    assert [(s["state"], s["rc"]) for s in spans] == [("ended", 3), ("ended", 0)]
    assert all(s["end"] >= s["start"] for s in spans)


def test_activity_log_rotates_at_its_cap_and_reads_both_generations(env, monkeypatch, tmp_path):
    from ttp import activity
    monkeypatch.setattr(activity, "MAX_BYTES", 2000)
    state = tmp_path / "state"
    ids = [_span(state, "box-a-device", 100.0 + i, 101.0 + i) for i in range(60)]
    log, old = state / activity.LOG, state / activity.OLD
    assert log.stat().st_size <= 2000 and old.stat().st_size <= 2000, "a generation passed the cap"
    got = {s["id"] for s in activity.spans(activity.entries(state))}
    assert ids[-1] in got and ids[0] not in got, "the oldest entries are dropped, the newest kept"
    assert activity.head("a  b\n" + "c" * 500) == ("a b " + "c" * 500)[:activity.HEAD]


def test_ttp_detach_logs_the_start_and_its_wrapper_logs_the_end(env):
    from ttp import activity
    p = make(env)
    run_dir = p.state / "runs" / "41"
    run_dir.mkdir(parents=True)
    r = subprocess.run([sys.executable, str(TTP), "detach", "job1", "--", "sh", "-c", "exit 4"],
                       env=_run_env(env, p, TTP_RUN_DIR=str(run_dir)), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    deadline = time.time() + 30
    while time.time() < deadline and not any(e["ev"] == "job_end" for e in activity.entries(p.state)):
        time.sleep(0.05)
    recs = activity.entries(p.state)
    start = next(e for e in recs if e["ev"] == "job")
    end = next(e for e in recs if e["ev"] == "job_end")
    assert start["id"] == end["id"] and start["job"] == "job1" and start["task"] == "7" and end["rc"] == 4
    assert "TTP_ACTIVITY" not in (run_dir / "job1.log").read_text()
    (span,) = activity.spans(recs)
    assert span["kind"] == "job" and span["state"] == "ended" and span["rc"] == 4


def test_machines_activity_filters_machine_and_window_and_reads_another_project_read_only(env, capsys, monkeypatch):
    from ttp import activity, cli
    from ttp import locks as lk
    from ttp import machines as mm
    for k in ("TTP_PROJECT", "TTP_RUN_ID", "TTP_RUN_DIR", "TTP_TASK"):
        monkeypatch.delenv(k, raising=False)
    p, q = make(env), _second_project(env)
    mm.add("box-a")
    mm.add("box-b")
    now = time.time()
    _span(p.state, "box-a-device", now - 600, now - 500, task="11")
    _span(q.state, "box-a-device", now - 300, now - 200, task="21", disruptive=True)
    _span(q.state, "box-b-device", now - 300, now - 200, task="22")
    _span(q.state, "box-a-device", now - 3 * 86400, now - 3 * 86400 + 60, task="23")   # long before the window
    held = lk.try_take([q.state / "locks" / "box-a-device.0.lock"], "task #24 (run 5)", "flash firmware")
    ticket = lk.enqueue(q.state / "locks", "box-a-device", "task #25 (run 6)")
    try:
        before = _snapshot(q.base)
        cli.main(["machines", "activity", "box-a", "--since", "1h", "--json"])
        view = json.loads(capsys.readouterr().out)
        cli.main(["machines", "activity", "box-a", "--since", "1h"])
        text = capsys.readouterr().out
        assert _snapshot(q.base) == before, "reading another project's activity wrote its state"
    finally:
        lk.dequeue(ticket)
        held.close()
    assert sorted(s["task"] for s in view["activity"]) == ["11", "21"], "the machine and window filters"
    assert [h["holder"] for h in view["holders"]] == ["task #24 (run 5)"]
    assert [w["holder"] for w in view["waiters"]] == ["task #25 (run 6)"]
    assert "DISRUPTIVE lock box-a-device: x task #21" in text and "holds box-a-device" in text
    assert "task #22" not in text and "task #23" not in text
    with pytest.raises(SystemExit):
        cli.main(["machines", "activity", "box-a", "--since", "soon"])


def test_reboot_note_lists_activity_before_the_boot_disruptive_first_within_its_line_cap(env):
    from ttp import activity
    from ttp.prompts import _reboot_note
    p = make(env)
    at = time.time() - 60
    _span(p.state, "box-a-device", at - 1700, at - 1650, task="90", disruptive=True)
    for i in range(8):
        _span(p.state, "box-a-device", at - 900 + i * 60, at - 880 + i * 60, task=str(100 + i))
    _span(p.state, "box-a-device", at - 120, None, task="200")   # cut off by the reboot: no release
    _span(p.state, "box-a-device", at - 7200, at - 7100, task="300")   # before the window
    _span(p.state, "box-a-device", at - 100, at - 50, task="400", host="elsewhere")   # another host
    note = _reboot_note({"at": at}, "UTC")
    lines = [ln for ln in note.splitlines() if ln.startswith("- ")]
    assert len(lines) == activity.REBOOT_LINES + 1 and lines[-1].startswith("- ... 5 more")
    assert lines[0].startswith("- DISRUPTIVE") and "task #90" in lines[0]
    assert "task #200" in lines[1] and "no end logged" in lines[1]
    assert "task #300" not in note and "task #400" not in note
    assert "Locks and detached jobs" not in _reboot_note({"at": at - 86400 * 5}, "UTC")


def test_spans_join_an_end_logged_before_its_start_and_read_a_job_rc_file_as_a_number(tmp_path):
    from ttp import activity
    rc = tmp_path / "j.rc"
    rc.write_text("4\n")
    recs = [{"ev": "job_end", "id": "a", "end": 20, "rc": 3},
            {"ev": "job", "id": "a", "start": 10, "host": "h"},
            {"ev": "job", "id": "b", "start": 11, "host": "h", "rcfile": str(rc)}]
    a, b = activity.spans(recs, here="h")
    assert (a["state"], a["end"], a["rc"]) == ("ended", 20, 3)
    assert (b["state"], b["rc"]) == ("ended", 4)


def test_an_open_take_whose_pid_is_a_kernel_thread_or_another_namespaces_is_unknown_not_running(monkeypatch):
    from ttp import activity, locks
    me, ns, b = os.getpid(), locks._pid_ns(), "b"
    k1, k2 = [k for k in (2, 13, 14) if k != me][:2]   # under `ttp sandbox` this test may be pid 2 itself
    monkeypatch.setattr(locks, "kthread", lambda pid, proc="/proc": int(pid) in (k1, k2))
    monkeypatch.setattr(activity, "_same_boot", lambda boot: boot == "b")
    recs = [{"ev": "take", "id": "old13", "host": "h", "start": 1, "pid": k2, "boot": b},   # logged before pidns
            {"ev": "take", "id": "ns2", "host": "h", "start": 2, "pid": k1, "pidns": ns, "boot": b},
            {"ev": "take", "id": "other", "host": "h", "start": 3, "pid": me, "pidns": "pid:[other]", "boot": b},
            {"ev": "take", "id": "none", "host": "h", "start": 4, "pid": me, "pidns": None, "boot": b},
            {"ev": "take", "id": "mine", "host": "h", "start": 5, "pid": me, "pidns": ns, "boot": b},
            {"ev": "take", "id": "legacy", "host": "h", "start": 6, "pid": me, "boot": b}]
    got = {s["id"]: s["state"] for s in activity.spans(recs, here="h")}
    assert got == {"old13": "unknown", "ns2": "unknown", "other": "unknown", "none": "unknown",
                   "mine": "running", "legacy": "running"}
