# Lost test worker tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new worker-loss tests (workerloss) here, not there.
from __future__ import annotations


def _lost_log(statuses, left=0, failed=None, passed=881):
    """A forked runner's output: one 'ended' failure per worker wait status, `left` tests no worker
    took, and a summary counting `failed` (default: just the lost tests)."""
    lines = ["=" * 30 + " FAILURES " + "=" * 30]
    for i, s in enumerate(statuses):
        lines += [f"{'_' * 20} test_{i}[a b] {'_' * 20}",
                  f"the test worker process {100 + i} ended (wait status {s}) before this test finished"]
    for i in range(left):
        lines += [f"_ test_left_{i} _", "no test worker was left to run this test"]
    n = len(statuses) + left if failed is None else failed
    return "\n".join(lines + [f"========== {n} failed, {passed} passed in 79.49s (0:01:19) =========="]) + "\n"


def test_worker_loss_alone_with_clean_exits_is_infra_and_nothing_else_is():
    from ttp import workerloss
    loss = workerloss.lost(_lost_log([0, 0, 0], left=5920))   # run 10050's shape: 5923 lost, all status 0
    assert loss == {"workers": {100: 0, 101: 0, 102: 0}, "tests": 5923, "other": 0}
    assert workerloss.only(loss) and workerloss.infra(loss)
    extra = workerloss.lost(_lost_log([0], left=141, failed=142 + 2))   # run 10051: 2 real failures too
    assert extra["other"] == 2 and not workerloss.only(extra) and not workerloss.infra(extra)
    for status in (9, -9, 256, "None"):   # a signal, a non-zero exit, an unknown status: maybe the change
        crash = workerloss.lost(_lost_log([0, status]))
        assert workerloss.only(crash) and not workerloss.infra(crash), status
    assert workerloss.lost("===== 3 failed, 10 passed in 1.00s =====\n") is None, "no lost worker named"
    assert workerloss.lost("") is None and not workerloss.only(None) and not workerloss.infra(None)
    nosum = workerloss.lost("\n".join(_lost_log([0]).splitlines()[:3]))
    assert nosum["other"] is None and not workerloss.only(nosum), "no summary counts the rest: no rerun"
    # Only a lost test's own failure section counts: a diff or summary line quoting it never does.
    quoted = ("_ test_parser _\n>       assert lost(x) is None\n"
              "E       the test worker process 7 ended (wait status 0) before this test finished\n"
              "E       no test worker was left to run this test\n"
              "FAILED tests/t.py::test_left_0 - no test worker was left to run this test\n")
    assert workerloss.lost(quoted + "===== 1 failed in 1.00s =====\n") is None
    twice = workerloss.lost(_lost_log([0], left=1, failed=3) + quoted)
    assert twice["tests"] == 2 and twice["other"] == 1 and not workerloss.only(twice), twice
    errs = workerloss.lost(_lost_log([0]).replace("1 failed,", "1 failed, 1 error,"))
    assert errs["other"] == 1 and not workerloss.only(errs), "an error besides the loss is a real failure"


def test_worker_loss_collector_reads_as_the_whole_output_and_keeps_no_quoted_line():
    """A runner that keeps only its last lines feeds a Collector: lost() reads the same from it as from
    the whole output; a diff or short-summary line quoting the runner's message never counts."""
    from ttp import workerloss
    quoted = ("_ test_parser _\n>       assert lost(x) is None\n"
              "E       the test worker process 7 ended (wait status 0) before this test finished\n"
              "FAILED tests/t.py::test_left_0 - no test worker was left to run this test\n")
    for text in (_lost_log([0, 0], left=3), _lost_log([0, 9]), _lost_log([0], left=1, failed=3),
                 quoted + "===== 1 failed in 1.00s =====\n", "lots of output\r\n" * 50 + _lost_log([0], left=2)):
        c = workerloss.Collector()
        for line in text.splitlines(keepends=True):
            c.feed(line)
        assert workerloss.lost(c.text()) == workerloss.lost(text.replace("\r\n", "\n")), text[-200:]
        c.reset()
        assert c.text() == "" and workerloss.lost(c.text()) is None


def test_worker_loss_marker_round_trips_and_a_broken_one_reads_as_none(tmp_path):
    from ttp import workerloss
    loss = workerloss.lost(_lost_log([0, 0]))
    workerloss.write_marker(tmp_path, loss, "pytest -q")
    m = workerloss.marker(tmp_path)
    assert m["check"] == "pytest -q" and m["tests"] == 2 and m["workers"] == {100: 0, 101: 0}
    assert "process 100 (wait status 0)" in workerloss.line(m, m["check"])
    for bad in ("{", "[]", '{"workers": 3}'):
        (tmp_path / workerloss.MARKER).write_text(bad)
        assert workerloss.marker(tmp_path) is None, bad
    workerloss.clear(tmp_path)
    assert not (tmp_path / workerloss.MARKER).exists() and workerloss.marker(tmp_path) is None
    workerloss.write_marker(None, loss, "x")   # no run dir: nothing written, nothing raised
    workerloss.clear(None)


def _lost_check(tmp_path, runs: list[str]):
    """A check command whose n-th run prints `runs[n]`'s log (the last one repeats): '' exits 0,
    anything else exits 1. Returns its argv and how many times it ran."""
    logs = []
    for i, text in enumerate(runs):
        f = tmp_path / f"run{i}.log"
        f.write_text(text)
        logs.append(f)
    count = tmp_path / "count"
    script = tmp_path / "check.sh"
    script.write_text(f"""n=$(cat {count} 2>/dev/null || echo 0)
echo $((n + 1)) > {count}
last={len(runs) - 1}
[ "$n" -gt "$last" ] && n=$last
f={tmp_path}/run$n.log
[ -s "$f" ] || exit 0
cat "$f"
exit 1
""")
    return ["sh", str(script)], lambda: int(count.read_text()) if count.exists() else 0


def test_ttp_checks_reruns_a_check_whose_only_failures_are_a_lost_worker_once_and_passes(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, workerloss
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    argv, ran = _lost_check(tmp_path, [_lost_log([0], left=3), ""])
    (run / workerloss.MARKER).write_text('{"workers": {}}')   # an older run's marker
    cli.main(["checks", "--", *argv])
    out = capsys.readouterr().out
    assert ran() == 2 and out.count("a test worker was lost mid-run (4 tests not run") == 1, out
    assert json.loads((run / "checks.json").read_text())["passed"] is True
    assert not (run / workerloss.MARKER).exists(), "a run that did not stop at a lost worker clears it"


def test_ttp_checks_lost_workers_again_on_the_rerun_are_an_infra_fault_exit_75(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, workerloss
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    argv, ran = _lost_check(tmp_path, [_lost_log([0], left=3)])
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", *argv])
    assert e.value.code == 75 and ran() == 1 + workerloss.RERUNS, "one rerun, never a loop"
    cap = capsys.readouterr()
    assert "infrastructure fault: test workers were lost mid-run again" in cap.err, cap.err
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["infra_fault"] == "test-worker-lost", rec
    m = workerloss.marker(run)
    assert m and m["tests"] == 4 and m["workers"] == {100: 0}
    (run / cli.CHECKS_RC).write_text("75\n")
    cli._checks_result(run)
    assert "infrastructure fault: test workers were lost" in capsys.readouterr().out


def test_ttp_checks_a_lost_worker_beside_a_real_failure_or_a_crash_twice_is_a_failure(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, workerloss
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    argv, ran = _lost_check(real, [_lost_log([0], left=3, failed=6)])
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", *argv])
    assert e.value.code == 1 and ran() == 1, "a real failure besides the loss: no rerun, a failure"
    assert "infra_fault" not in json.loads((run / "checks.json").read_text())
    assert not (run / workerloss.MARKER).exists()
    crash = tmp_path / "crash"
    crash.mkdir()
    argv, ran = _lost_check(crash, [_lost_log([0, 9])])
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", *argv])
    assert e.value.code == 1 and ran() == 2, "a killed worker is rerun once, then stays a failure"
    assert "infra_fault" not in json.loads((run / "checks.json").read_text())
    assert not (run / workerloss.MARKER).exists()
    capsys.readouterr()


def test_push_run_one_reruns_a_lost_worker_once_holds_a_second_clean_loss_and_fails_the_rest(tmp_path):
    """`ttp push` and `ttp land` (push._run_one): a lost worker alone runs the check once more; lost
    again, every worker exiting 0, it is the host (BUSY with a retry_when), never CHECKS_FAILED. A
    crash twice, or a loss beside real failures, still fails."""
    import os
    from ttp import push, workerloss
    cases = {"pass": ([_lost_log([0], left=3), ""], 0, 2),
             "infra": ([_lost_log([0, 0], left=3)], push.BUSY, 1 + workerloss.RERUNS),
             "crash": ([_lost_log([0, 9])], push.CHECKS_FAILED, 2),
             "real": ([_lost_log([0], left=3, failed=6)], push.CHECKS_FAILED, 1)}
    for name, (runs, want, n) in cases.items():
        d = tmp_path / name
        d.mkdir()
        argv, ran = _lost_check(d, runs)
        said = []
        assert push._run_one(" ".join(argv), d, dict(os.environ), said.append) == want, (name, said)
        assert ran() == n, (name, said)
        reran = [s for s in said if s.startswith("a test worker was lost mid-run")]
        assert len(reran) == (n - 1), (name, said)
        infra = [s for s in said if s.startswith("infrastructure fault: test workers were lost")]
        assert bool(infra) == (name == "infra"), (name, said)


def test_push_run_one_infra_hold_names_a_time_retry_when(tmp_path):
    import os
    from ttp import push
    argv, ran = _lost_check(tmp_path, [_lost_log([0], left=1)])
    said = []
    assert push._run_one(" ".join(argv), tmp_path, dict(os.environ), said.append) == push.BUSY
    assert said[-1].startswith("infrastructure fault: test workers were lost mid-run again") and \
        "; not pushing. Hand off `waiting`; retry_when: test \"$(date +%s)\" -ge " in said[-1], said


def _loss_batch(env, monkeypatch, name, runs):
    """A push queue batch of one entry whose push check prints `runs` (see _lost_check) with the entry's
    change and passes on the tip alone: (exit code, marker, how many times it ran with the change)."""
    d = env["tmp"] / name
    d.mkdir()
    argv, ran = _lost_check(d, runs)
    p, repo, origin, other = _bump_setup(env, monkeypatch, [f"[ -e plugins/p/f1.txt ] || exit 0; {' '.join(argv)}"])
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})]), monkeypatch)
    return rc, m, ran()


def test_push_batch_reruns_a_check_whose_only_failures_are_a_lost_worker_and_pushes(env, monkeypatch, capsys):
    from ttp import push
    rc, m, ran = _loss_batch(env, monkeypatch, "pass", [_lost_log([0], left=3), ""])
    assert rc == 0 and m["outcome"] == "pushed" and ran == 2, m
    assert m["checks"]["worker_loss_reruns"] == 1
    assert capsys.readouterr().out.count("a test worker was lost mid-run (4 tests not run") == 1


def test_push_batch_holds_on_lost_workers_again_and_requeues_its_entries_not_failed(env, monkeypatch, capsys):
    from ttp import push, workerloss
    rc, m, ran = _loss_batch(env, monkeypatch, "infra", [_lost_log([0], left=3)])
    assert rc == push.BUSY and m["outcome"] == "worker_loss" and ran == 1 + workerloss.RERUNS, m
    assert _statuses(m) == ["requeued"], m
    assert m["checks"]["worker_loss"] == {"check": m["checks"]["worker_loss"]["check"], "tests": 4,
                                          "workers": {"100": 0}}
    out = capsys.readouterr().out
    assert "infrastructure fault: test workers were lost mid-run again" in out and "the batch is held, not failed" in out
    assert "check failed on" not in out


def test_push_batch_a_crash_twice_or_a_loss_beside_real_failures_still_fails(env, monkeypatch, capsys):
    rc, m, ran = _loss_batch(env, monkeypatch, "crash", [_lost_log([0, 9])])
    assert m["outcome"] != "worker_loss" and _statuses(m) == ["check_failed"] and ran == 2, m
    assert capsys.readouterr().out.count("a test worker was lost mid-run") == 1, "rerun once, then a failure"


def test_push_batch_a_loss_beside_real_failures_fails_without_a_rerun(env, monkeypatch, capsys):
    rc, m, ran = _loss_batch(env, monkeypatch, "real", [_lost_log([0], left=3, failed=6)])
    assert m["outcome"] != "worker_loss" and _statuses(m) == ["check_failed"] and ran == 1, m
    assert "a test worker was lost mid-run" not in capsys.readouterr().out


def test_a_worker_loss_batch_keeps_its_approvals_counts_no_try_and_backs_the_queue_off(env, monkeypatch):
    """(the queue) A batch that ended worker_loss puts its approvals back as approved, no try counted,
    and the queue waits push.WORKER_LOSS_S before the next batch (a quieter host), then resumes."""
    from ttp import push, pushq
    s = _pq(env, monkeypatch)
    _pq_plan(s, outcome="worker_loss", row="requeued")
    _pq_hand_off(env, s)
    _pq_batch(s)
    _pq_tend(s)
    assert s.p.db.one("SELECT status, tries FROM push_queue") == {"status": "approved", "tries": 0}
    assert s.p.db.task(s.review)["status"] != "failed"
    now = time.time()
    rows, why = pushq.due(s.p, now)
    assert rows == [] and why.startswith("backing off until"), why
    assert pushq.due(s.p, now + push.WORKER_LOSS_S + 10 ** 6)[0], "resumes"
