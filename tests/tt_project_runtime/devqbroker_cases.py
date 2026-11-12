# Device-broker tests of `ttp devq` (devq.py) for tests/test_tt_project_runtime.py, which runs this file in
# its own namespace (see _include_cases there): helpers, fixtures and imports come from that file. Only a
# fake `tt-device-mcp` on PATH: it runs each queued job at once and preempts one wait while a flag file exists.
from __future__ import annotations


def _fake_broker(tmp_path, monkeypatch):
    """A fake broker on PATH; returns its folder (`calls`: its run-bg and kill calls; touch `preempt` to
    have the next wait end preempted)."""
    fake, bindir = tmp_path / "broker", tmp_path / "brokerbin"
    fake.mkdir()
    bindir.mkdir()
    b = bindir / "tt-device-mcp"
    b.write_text(f"""#!/bin/sh
D={shlex.quote(str(fake))}
case "$1" in
run-bg) echo "$*" >>"$D/calls"; for last; do :; done; n=$(( $(cat "$D/n" 2>/dev/null || echo 0) + 1 )); echo $n >"$D/n"
  sh -c "$last" >"$D/$n.out" 2>&1; echo $? >"$D/$n.rc"; echo "Job j$n queued (position: 0)";;
wait) n=${{2#j}}; cat "$D/$n.out"
  if [ -e "$D/preempt" ]; then rm "$D/preempt"; echo "--- Job $2 finished: preempted ---"; exit 1; fi
  r=$(cat "$D/$n.rc"); [ "$r" = 0 ] && s=completed || s=failed; echo "--- Job $2 finished: $s ---"; exit $r;;
kill) echo "kill $2" >>"$D/calls";;
esac
""")
    b.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return fake


def test_devq_on_a_broker_host_runs_preemptable_and_reruns_a_preempted_job_without_a_drop(devq_dir, tmp_path,
                                                                                         monkeypatch):
    fake = _fake_broker(tmp_path, monkeypatch)
    (fake / "preempt").touch()
    _devq_submit(devq_dir, {"max_drops": 1}, "j1", 'echo "preempts=$TTP_DEVQ_PREEMPTS limit=$TTP_DEVQ_TIMEOUT_S"')
    m = _devq_marker(devq_dir, "j1")
    assert m["status"] == "done" and m["preempts"] == 1 and m["drops"] == [] and m["attempts"] == 2, m
    calls = (fake / "calls").read_text().splitlines()
    assert len(calls) == 2 and all(c.startswith("run-bg --preemptable -t 1500 ") for c in calls), calls
    assert "preempts=0 limit=1500" in (devq_dir / "logs" / "j1.1.log").read_text()
    assert "preempts=1 limit=1500" in (devq_dir / "logs" / "j1.2.log").read_text()
    assert not (devq_dir / "drops.log").exists() and not list((devq_dir / "configs").iterdir())


def test_devq_on_a_broker_host_refuses_a_timeout_over_the_broker_cap_and_warns_on_unpreemptable_calls(
        devq_dir, tmp_path, monkeypatch):
    _fake_broker(tmp_path, monkeypatch)
    r = _devq("submit", devq_dir, json.dumps(DEVQ_FAST), json.dumps({"id": "long", "cmd": "true", "timeout_s": 1600}))
    assert r.returncode == 2 and "split the run into shorter jobs" in r.stderr, r.stderr
    bad = _devq_submit(devq_dir, {}, "own1", "tt-device-mcp run-bg -t 3000 python3 x.py")
    assert "lacks --preemptable" in bad.stderr and "needs -t 1500 or less" in bad.stderr, bad.stderr
    good = _devq_submit(devq_dir, {}, "own2", "tt-device-mcp run-bg --preemptable -t 1200 python3 x.py")
    assert "preemptable" not in good.stderr, good.stderr


def test_devq_reruns_a_job_that_yielded_and_fails_it_past_max_preempts(devq_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")   # no broker here: the yield marker alone
    _devq_submit(devq_dir, {"broker": "off"}, "y1",
                 '[ "$TTP_DEVQ_PREEMPTS" = 0 ] && date >"$TTP_DEVQ_YIELD_FILE"; echo "run $TTP_DEVQ_PREEMPTS"')
    m = _devq_marker(devq_dir, "y1")
    assert m["status"] == "done" and m["preempts"] == 1 and m["drops"] == [], m
    _devq_submit(devq_dir, {"broker": "off", "max_preempts": 2}, "y2", 'date >"$TTP_DEVQ_YIELD_FILE"')
    m = _devq_marker(devq_dir, "y2")
    assert m["status"] == "failed" and m["preempts"] == 3 and "preempted 3 times" in m["reason"], m
    assert not list((devq_dir / "running").glob("*.yielded"))
