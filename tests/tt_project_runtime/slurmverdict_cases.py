# slurmverdict: a Slurm job's model-free verdict (setup-box's box.py slurm verdict and the Slurm check spill).

_SV_LOG = ("==> build start\nbuild: reused\n==> build done rc=0\n==> test start\ncollected 10 items\n"
           "E       AssertionError: 3 != 4\nE       AssertionError: 5 != 4\n"
           "FAILED tests/test_x.py::test_a - AssertionError: 3 != 4\nFAILED tests/test_x.py::test_b\n"
           "==== 2 failed, 8 passed in 3.20s ====\n==> test done rc=1\n")


def test_a_slurm_verdict_names_the_failed_step_the_pytest_summary_the_failed_tests_and_distinct_errors():
    from ttp import slurmverdict
    v = slurmverdict.verdict(_SV_LOG, "FAILED", 1)
    assert (v["verdict"], v["step"], v["pytest"]) == ("failed", "test", "2 failed, 8 passed in 3.20s"), v
    assert v["failed_tests"] == ["tests/test_x.py::test_a", "tests/test_x.py::test_b"]
    assert v["errors"] == ["E       AssertionError: 3 != 4"], "a repeat differing only in numbers counts once"
    out = slurmverdict.lines(v)
    assert out[0] == "verdict: failed at step test; rc 1; FAILED" and "failed tests: tests/test_x.py::test_a" in out[2]
    assert " | " in slurmverdict.one_line(v)
    noisy = "".join(f"RuntimeError: kind {chr(97 + i)} of failure\n" for i in range(20))
    assert len(slurmverdict.verdict(noisy, "", 1)["errors"]) == slurmverdict.MAX_ERRORS


def test_a_slurm_verdict_tells_a_time_limit_or_cancel_from_a_failure_and_a_pass_from_an_unknown():
    from ttp import slurmverdict
    cut = "==> case:c2 start\n*** JOB 77 ON node1 CANCELLED AT 2026-01-01T00:00:00 DUE TO TIME LIMIT ***\n"
    v = slurmverdict.verdict(cut)
    assert (v["verdict"], v["step"]) == ("timeout", "case:c2"), v
    assert slurmverdict.verdict("==> test start\n", "TIMEOUT")["verdict"] == "timeout"
    assert slurmverdict.verdict("==> test start\n", "PREEMPTED")["verdict"] == "cancelled"
    assert slurmverdict.verdict("*** JOB 7 ON n CANCELLED AT 2026-01-01T00:00:00 ***\n")["verdict"] == "cancelled"
    ok = "==> build start\n==> build done rc=0\n"
    assert slurmverdict.verdict(ok, "COMPLETED", 0)["verdict"] == "passed"
    assert slurmverdict.verdict(ok, "COMPLETED")["verdict"] == "passed"
    assert slurmverdict.verdict(ok, "", 3)["verdict"] == "failed", "the recorded exit code wins over clean steps"
    assert slurmverdict.verdict("==> build start\n==> build done rc=2\n", "COMPLETED", 0)["verdict"] == "failed"
    assert slurmverdict.verdict("some output\n")["verdict"] == "unknown"
    assert slurmverdict.verdict("")["verdict"] == "unknown" and slurmverdict.verdict("", "FAILED")["verdict"] == "failed"


def test_a_failed_slurm_check_prints_its_verdict_line_and_a_passing_one_does_not(env, tmp_path):
    repo = _rc_repo(tmp_path)
    fake, _ = _slurm_fakes(tmp_path)
    cmd = ("echo 'E       ValueError: bad input'; echo 'FAILED tests/test_y.py::test_c - ValueError'; "
           "echo '=== 1 failed, 4 passed in 0.50s ==='; exit 1")
    rc, out = _slurm_run(tmp_path, repo, cmd, fake)
    assert rc == 1, out
    line = next(ln for ln in out.splitlines() if "verdict:" in ln)
    assert line.startswith("ttp slurm check: verdict: failed; rc 1") and "pytest: 1 failed, 4 passed in 0.50s" in line
    assert "tests/test_y.py::test_c" in line and "error: E       ValueError: bad input" in line, line
    rc, out = _slurm_run(tmp_path, repo, "echo fine", fake)
    assert rc == 0 and "verdict:" not in out, out
