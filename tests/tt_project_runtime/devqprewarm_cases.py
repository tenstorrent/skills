# Prewarm and chunk-guard tests of `ttp devq` (devq.py) for tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
# Fake runners only (a shell command and a state folder): no device, no broker, no device job.
from __future__ import annotations


def _prewarm_cfg(tmp_path, **cfg):
    return {"broker": "off", "prewarm": f"echo $TTP_DEVQ_JOB >>{shlex.quote(str(tmp_path / 'warm'))}", **cfg}


def test_devqprewarm_chunk_guard_refuses_a_broker_call_over_the_limit_in_any_spelling():
    from ttp import devq
    for cmd in ("tt-device-mcp run-bg --preemptable -t 3000 python3 x.py",
                "tt-device-mcp run-bg --preemptable -t3000 python3 x.py",
                "tt-device-mcp run --preemptable --timeout=2000 python3 x.py",
                "tt-device-mcp run-bg --preemptable -t '2000' python3 x.py",
                "cd /w && tt-device-mcp  run-bg -t 99999 --preemptable python3 x.py"):
        bad = devq.chunk_problems({"cmd": cmd})
        assert len(bad) == 1 and "checkpoint" in bad[0] and "1500" in bad[0], (cmd, bad)
    for cmd in ("tt-device-mcp run-bg --preemptable -t 1500 python3 x.py",
                "tt-device-mcp run-bg --preemptable -t $LIMIT python3 x.py",
                "python3 x.py -t 99999",             # not a broker call: another tool's -t
                "tt-device-mcp run -t 600 -- python3 -m pytest --timeout=3000 tests/",   # after `--`: the job's own flag
                "tt-device-mcp status -t 99999"):
        assert devq.chunk_problems({"cmd": cmd}) == [], cmd


def test_devqprewarm_submit_refuses_a_chunk_over_the_limit_with_checkpoint_advice(devq_dir):
    for spec in ({"timeout_s": 1600}, {"cmd": "tt-device-mcp run-bg --preemptable -t 4000 python3 x.py"}):
        r = _devq("submit", devq_dir, json.dumps({**DEVQ_FAST, "broker": "on"}),
                  json.dumps({"id": "long", "cmd": "true", **spec}))
        assert r.returncode == 2 and "checkpoint" in r.stderr and "shorter jobs" in r.stderr, r.stderr
    assert not (devq_dir / "queue").exists() or not list((devq_dir / "queue").iterdir())


def test_devqprewarm_config_refuses_a_prewarm_that_claims_the_device():
    from ttp import devq
    assert devq.config_problems("r", {"prewarm": "make -C build && ./warm.sh", "prewarm_policy": "refuse",
                                      "prewarm_timeout_s": 600}) == []
    assert devq.config_problems("r", {"prewarm": "tt-device-mcp status"}) == []
    for cmd in ("tt-device-mcp run-bg python3 warm.py", "/usr/bin/tt-device-mcp run python3 warm.py",
                '"tt-device-mcp" run-bg x', "b=tt-device-mcp; $b run-bg x",
                "tt-device-mcp status; tt-device-mcp run x"):
        probs = devq.config_problems("r", {"prewarm": cmd})
        assert len(probs) == 1 and "must not hold the device" in probs[0], (cmd, probs)
    bad = devq.config_problems("r", {"prewarm": 3, "prewarm_policy": "skip", "prewarm_timeout_s": -1})
    assert [x.split(":")[0] for x in bad] == ["device.runners.r.prewarm", "device.runners.r.prewarm_policy",
                                               "device.runners.r.prewarm_timeout_s"]


def test_devqprewarm_runs_once_per_job_before_the_job_starts_in_its_workdir(devq_dir, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    cfg = _prewarm_cfg(tmp_path, prewarm=f"pwd >{shlex.quote(str(tmp_path / 'where'))}; "
                                          f"echo warm >{shlex.quote(str(tmp_path / 'order'))}")
    _devq_submit(devq_dir, cfg, "p1", f"cat {shlex.quote(str(tmp_path / 'order'))} >{shlex.quote(str(tmp_path / 'seen'))}",
                 workdir=str(work))
    assert _devq_marker(devq_dir, "p1")["status"] == "done"
    assert (tmp_path / "seen").read_text() == "warm\n", "the job started after the prewarm finished"
    assert (tmp_path / "where").read_text().strip() == str(work.resolve())


def test_devqprewarm_one_prewarm_per_job_across_a_yield_rerun(devq_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _devq_submit(devq_dir, _prewarm_cfg(tmp_path), "y1",
                 '[ "$TTP_DEVQ_PREEMPTS" = 0 ] && date >"$TTP_DEVQ_YIELD_FILE"; echo "run $TTP_DEVQ_PREEMPTS"')
    m = _devq_marker(devq_dir, "y1")
    assert m["status"] == "done" and m["preempts"] == 1, m
    assert (tmp_path / "warm").read_text().split() == ["y1"], "prewarmed once, not again after the preemption"


def test_devqprewarm_failed_prewarm_warns_by_default_and_refuses_on_policy(devq_dir, tmp_path):
    from ttp import devq
    fail = _prewarm_cfg(tmp_path, prewarm="echo no cache; exit 3")
    _devq_submit(devq_dir, fail, "w1", "true")
    assert _devq_marker(devq_dir, "w1")["status"] == "done", "a failed warm-up does not stop the job"
    assert "w1: prewarm exit 3: no cache" in (devq_dir / "runner.log").read_text()
    _devq_submit(devq_dir, {**fail, "prewarm_policy": "refuse"}, "w2", "true")
    m = _devq_marker(devq_dir, "w2")
    assert m["status"] == "failed" and "not started: prewarm exit 3: no cache" in json.dumps(m), m
    slow = {**fail, "prewarm": "sleep 30", "prewarm_policy": "refuse", "prewarm_timeout_s": 0.3}
    _devq_submit(devq_dir, slow, "w3", "true")
    assert "prewarm exit 124" in json.dumps(_devq_marker(devq_dir, "w3"))


def test_devqprewarm_runner_refuses_a_prewarm_that_calls_the_broker_even_when_config_slipped_through(
        devq_dir, tmp_path):
    cfg = _prewarm_cfg(tmp_path, prewarm="tt-device-mcp run-bg python3 warm.py")
    _devq_submit(devq_dir, cfg, "b1", "true")
    m = _devq_marker(devq_dir, "b1")
    assert m["status"] == "failed" and "not started: prewarm" in json.dumps(m) and "hold the device" in json.dumps(m), m
    assert not (tmp_path / "warm").exists()
