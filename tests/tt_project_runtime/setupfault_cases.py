# Checks that fail before any test ran (setupfault: a work dir gone, a bad archive, no free pool slot) tests of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file.
from __future__ import annotations


def _setup_check(d, runs: list[str]):
    """A check command whose n-th run prints runs[n] (the last one repeats): '' exits 0, anything else
    exits 1. Returns its argv and how many times it ran."""
    for i, text in enumerate(runs):
        (d / f"setup{i}.log").write_text(text)
    count, script = d / "setup-count", d / "setup-check.sh"
    script.write_text(f"""n=$(cat {count} 2>/dev/null || echo 0)
echo $((n + 1)) > {count}
[ "$n" -gt {len(runs) - 1} ] && n={len(runs) - 1}
f={d}/setup$n.log
[ -s "$f" ] || exit 0
cat "$f"
exit 1
""")
    return ["sh", str(script)], lambda: int(count.read_text()) if count.exists() else 0


def _head_gone(d) -> str:
    """A pool copy whose head dir is gone, as a shard prints it (the path outside the repository)."""
    return (f"pool-copy: line 154: cd: {d}/scratch/gate.Ab12/head: No such file or directory\n"
            "tar: This does not look like a tar archive\ntar: Exiting with failure status due to previous errors\n"
            "pool-copy: copy to pool-host failed\n")


_BAD_TAR = "tar: Unexpected EOF in archive\ntar: Error is not recoverable: exiting now\n"
_STARVED = "gate: head: waiting 44 min for a pool: no CPU pool free (every pool held)\n"


def test_setupfault_names_only_faults_before_any_test_ran(tmp_path):
    """found(): a dir gone outside the repository, an empty or corrupt archive, or no free pool slot, with no
    test verdict anywhere in the output; a dir missing inside the repository, a relative one, or any verdict
    (a pytest count, a result line, TAP, PASS/FAIL, a traceback) leaves a real failure one."""
    from ttp import setupfault
    repo = tmp_path / "repo"
    repo.mkdir()
    found = lambda text: setupfault.found(setupfault.scan(text), repo)
    assert found(_head_gone(tmp_path))["kind"] == "setup"
    assert found(_head_gone(tmp_path))["lines"][0].endswith("No such file or directory")
    for bad in (_BAD_TAR, "gzip: stdin: not in gzip format\n", "gzip: stdin: unexpected end of file\n",
                "rsync error: some files could not be transferred (code 23)\n"):
        assert found(bad)["kind"] == "setup", bad
    assert found(_STARVED)["kind"] == "pool" and found("no free slot on any test host\n")["kind"] == "pool"
    assert found(_head_gone(tmp_path) + _STARVED)["kind"] == "pool", "the pool is starved now: wait, never rerun at once"
    assert found(f"x: line 3: cd: {repo}/build: No such file or directory\n") is None, "the change's own dir"
    assert found("x: line 3: cd: build: No such file or directory\n") is None
    assert found("exit status 1\nsomething broke\n") is None
    for verdict in ("1 failed, 3 passed in 2.1s", "FAILED tests/test_a.py::test_b - assert 0", "not ok 3 - copies",
                    "test_hsc.sh: FAIL", "PASS test_one.sh", "Traceback (most recent call last):", "2 errors in 1.0s"):
        assert found(_head_gone(tmp_path) + verdict + "\n") is None, verdict
        assert found(verdict + "\n" + _BAD_TAR) is None, verdict
    assert setupfault.scan("").verdict is False and found("") is None
    # a gate's own shard lines saying no test ran (its local fallback after a starved pool) are no verdict
    gate = ("no CPU pool free after 120 s; running shards locally\n" + _head_gone(tmp_path)
            + "FAIL shard 3 (infra); no verdict\nhead run did not complete (infra); no verdict\n")
    assert found(gate)["kind"] == "setup", "a pool wait that ran on and then lost its head dir reruns"
    assert found(_head_gone(tmp_path) + "FAIL shard 3 (infra); no verdict\n")
    # the kind is the last fault seen, past the KEPT lines too
    assert found(_STARVED + _head_gone(tmp_path))["kind"] == "setup"
    assert found(_head_gone(tmp_path) * 3 + _STARVED)["kind"] == "pool"
    assert found(_STARVED + f"x: cd: {repo}/b: No such file or directory\n")["kind"] == "pool"
    assert found("FAIL shard 1 (infra); no verdict\n")["kind"] == "setup"
    assert found(gate + "FAIL tests/test_a.sh\n") is None, "a real FAIL from another shard stays a failure"
    assert found("FAIL shard 1 (infra); no verdict\n1 failed in 0.2s\n") is None


def test_push_run_one_reruns_a_check_that_failed_before_any_test_ran_and_holds_it_after(tmp_path):
    """`ttp push` (push._run_one): a check whose head dir was gone before the pool copy, or whose archive was
    corrupt, runs again (setupfault.RERUNS times); passing then, it passes; still so, it is an infrastructure
    fault: BUSY with a retry_when, never CHECKS_FAILED and never a pass. No free pool slot waits at once (BUSY,
    no rerun). A real test failure fails at once, also after a setup fault on an earlier run."""
    from ttp import push, setupfault
    repo = tmp_path / "repo"
    repo.mkdir()
    n = setupfault.RERUNS + 1
    cases = {"gone_then_pass": ([_head_gone(tmp_path), ""], 0, 2),
             "gone": ([_head_gone(tmp_path)], push.BUSY, n),
             "bad_tar": ([_BAD_TAR], push.BUSY, n),
             "starved": ([_STARVED], push.BUSY, 1),
             "then_real": ([_head_gone(tmp_path), "FAILED tests/test_a.py::test_b\n1 failed in 0.1s\n"], push.CHECKS_FAILED, 2),
             "real": (["test_hsc.sh: FAIL\n" + _head_gone(tmp_path)], push.CHECKS_FAILED, 1),
             "plain": (["it broke\n"], push.CHECKS_FAILED, 1)}
    for name, (runs, want, ran_n) in cases.items():
        d = tmp_path / name
        d.mkdir()
        argv, ran = _setup_check(d, runs)
        said = []
        assert push._run_one(" ".join(argv), repo, dict(os.environ), said.append) == want, (name, said)
        assert ran() == ran_n, (name, said)
        assert sum("running it again" in s for s in said) == (ran_n - 1 if name != "starved" else 0), (name, said)
        infra = [s for s in said if s.startswith("infrastructure fault: ")]
        assert bool(infra) == (want == push.BUSY), (name, said)
        if infra:
            assert "no test ran, so the change is neither passed nor failed" in infra[0], infra
            assert "; not pushing. Hand off `waiting`; retry_when: test \"$(date +%s)\" -ge " in infra[0], infra
            assert ("no test pool had a free slot" in infra[0]) == (name == "starved"), infra


def test_push_run_one_a_check_ended_at_its_pool_wait_bound_waits_never_fails(tmp_path, monkeypatch):
    """A check the push ended at its pool-wait bound (push.POOL_STARVED) ran no test: BUSY, never rerun at once,
    never CHECKS_FAILED; a real exit code from _timed_wait still fails."""
    from ttp import push
    repo = tmp_path / "repo"
    repo.mkdir()
    for rc, want in ((push.POOL_STARVED, push.BUSY), (1, push.CHECKS_FAILED), (124, push.CHECKS_FAILED)):
        calls = []
        monkeypatch.setattr(push, "_timed_wait", lambda proc, waits, cmd, say, collect=None, rc=rc: (calls.append(1), proc.wait(), rc)[2])
        said = []
        assert push._run_one("true", repo, dict(os.environ), said.append) == want, (rc, said)
        assert len(calls) == 1, (rc, said)
        assert any("no test pool had a free slot" in s for s in said) == (want == push.BUSY), said


def test_ttp_checks_a_check_that_failed_before_any_test_ran_is_an_infra_fault_exit_75(env, tmp_path, monkeypatch, capsys):
    """`ttp checks`: a setup fault runs again and passes; still one after setupfault.RERUNS reruns it exits 75
    with infra_fault no-test-ran, never a pass; a real failure stays one (no rerun, no infra_fault)."""
    from ttp import cli, setupfault
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (tmp_path / "a").mkdir()
    argv, ran = _setup_check(tmp_path / "a", [_head_gone(tmp_path), ""])
    cli.main(["checks", "--", *argv])
    assert ran() == 2 and json.loads((run / "checks.json").read_text())["passed"] is True
    (tmp_path / "b").mkdir()
    argv, ran = _setup_check(tmp_path / "b", [_BAD_TAR])
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", *argv])
    assert e.value.code == 75 and ran() == setupfault.RERUNS + 1, "bounded reruns, never a loop"
    cap = capsys.readouterr()
    assert "infrastructure fault: " in cap.err and "neither passed nor failed" in cap.err, cap.err
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["infra_fault"] == setupfault.INFRA, rec
    (tmp_path / "c").mkdir()
    argv, ran = _setup_check(tmp_path / "c", ["1 failed in 0.2s\n" + _BAD_TAR])
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", *argv])
    assert e.value.code not in (0, 75) and ran() == 1
    assert "infra_fault" not in json.loads((run / "checks.json").read_text())


def test_push_batch_holds_a_check_that_failed_before_any_test_ran_and_requeues_its_entries(env, monkeypatch, capsys):
    from ttp import push, setupfault
    d = env["tmp"] / "setup"
    d.mkdir()
    argv, ran = _setup_check(d, [_head_gone(d)])
    p, repo, origin, other = _bump_setup(env, monkeypatch, [f"[ -e plugins/p/f1.txt ] || exit 0; {' '.join(argv)}"])
    rc, m = _run_batch(p, _batch_marker(p, [_entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})]), monkeypatch)
    assert rc == push.BUSY and m["outcome"] == "setup_fault" and ran() == setupfault.RERUNS + 1, m
    assert _statuses(m) == ["requeued"], m
    assert m["checks"]["setup_fault"]["kind"] == "setup", m
    out = capsys.readouterr().out
    assert "the batch is held, not failed" in out and "check failed on" not in out, out


def test_repeated_setup_fault_holds_of_one_check_raise_one_alert_until_that_check_passes(env, monkeypatch):
    """A batch held setup_fault (a check that failed before any test ran, after its reruns) goes back to the
    queue. Two holds of one check raise nothing; the third in a row raises one high alert naming the check, the
    fault lines and the batches. Another check's hold neither counts nor clears it. A batch that passes the
    check clears its count and the alert; a pushed batch clears every count. delivery.push_setup_fault_escalate_after
    0 turns it off."""
    from ttp import alerts, coordinator, push, pushq
    s = _pq(env, monkeypatch)
    db = s.p.db
    _pq_hand_off(env, s)
    a, b = "make test", "make lint"

    def held(cmd, line="cd: /srv/gate/head: No such file or directory"):
        _pq_plan(s, outcome="setup_fault", row="requeued",
                 checks={"runs": 1, "seconds": 2.0, "setup_fault": {"check": cmd, "kind": "setup", "lines": [line]}})
        bid = _pq_batch(s)
        _pq_tend(s)
        return bid

    def ran(outcome, row, passed):
        _pq_plan(s, outcome=outcome, row=row, checks={"runs": 1, "seconds": 2.0, "passed": passed})
        _pq_batch(s)
        _pq_tend(s)

    alerted = lambda cmd: db.q("SELECT text, severity FROM messages WHERE kind='alert' AND ref=?", (pushq.setup_key(cmd),))
    count = lambda cmd: ((db.kv(pushq.KV) or {}).get("setup") or {}).get(cmd, {}).get("n", 0)
    holds = lambda cmd: alerts.holds(db, pushq.setup_key(cmd), 0, time.time())
    runs = [held(a), held(a)]
    assert (count(a), alerted(a), holds(a)) == (2, [], False), "two holds raise nothing"
    held(b)   # another check: counted apart, never resets or raises the first
    assert (count(a), count(b), alerted(b)) == (2, 1, [])
    runs.append(held(a, "tar: This does not look like a tar archive"))
    [msg] = alerted(a)
    assert a in msg["text"] and "/srv/gate/head" in msg["text"] and "tar archive" in msg["text"], msg
    assert all(str(r) in msg["text"] for r in runs) and holds(a) and not holds(b)
    isolate = lambda bid: push._read(s.p.state / push.DETACHED / f"{bid}.json").get("setup_isolate")
    assert isolate(runs[-1]) is None, "the batch before the alert holds as before"
    assert isolate(held(a)) == [a], "after the alert, the next batch isolates on that check's setup fault"
    assert count(a) == 4 and len(alerted(a)) == 1, "one alert, not one per hold"
    assert "torn" not in db.kv(pushq.KV), "a setup_fault hold is no script_torn one"
    ran("moved", "requeued", [b])   # passing the other check clears only that one
    assert (count(a), count(b), holds(a)) == (4, 0, True)
    ran("moved", "requeued", [a])
    assert (count(a), holds(a)) == (0, False), "a pass of the check clears it"
    assert isolate(held(b)) is None, "and its isolation"
    held(a), held(a)
    assert count(a) == 2 and len(alerted(a)) == 1, "counting starts again after a pass"
    ran("pushed", "pushed", [])
    assert "setup" not in db.kv(pushq.KV), "a pushed batch clears every count"
    for bad in (-1, 1.5, "x"):
        with pytest.raises(ValueError):
            coordinator.push_queue_number("push_setup_fault_escalate_after", bad)


def test_setup_fault_escalation_off_raises_nothing(env, monkeypatch):
    from ttp import pushq
    s = _pq(env, monkeypatch, push_setup_fault_escalate_after=0)
    _pq_hand_off(env, s)
    for _ in range(4):
        _pq_plan(s, outcome="setup_fault", row="requeued",
                 checks={"runs": 1, "seconds": 2.0, "setup_fault": {"check": "make test", "kind": "pool", "lines": ["x"]}})
        _pq_batch(s)
        _pq_tend(s)
    assert not s.p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref=?", (pushq.setup_key("make test"),))


def test_after_the_setup_fault_alert_the_batch_isolates_the_entry_whose_check_hits_one(env, monkeypatch, capsys):
    """Entry 1's check always fails in setup (a dir outside the repository gone) while entries 2 and 3 are
    clean. Before the queue alerted on that check, the batch holds and requeues all three. Once it did
    (the marker's setup_isolate), the setup fault is the head's failed check: the search isolates entry 1,
    entries 2 and 3 push."""
    from ttp import push, setupfault
    d = env["tmp"] / "setup"
    d.mkdir()
    argv, ran = _setup_check(d, [_head_gone(d)])
    cmd = f"[ -e plugins/p/f1.txt ] || exit 0; {' '.join(argv)}"
    p, repo, origin, other = _bump_setup(env, monkeypatch, [cmd])
    heads = [_entry(repo, f"e{n}", {f"plugins/p/f{n}.txt": f"{n}\n"}) for n in (1, 2, 3)]
    rc, m = _run_batch(p, _batch_marker(p, heads, bid="b1"), monkeypatch)
    assert rc == push.BUSY and m["outcome"] == "setup_fault" and _statuses(m) == ["requeued"] * 3, m
    marker = _batch_marker(p, heads, bid="b2")
    m = push._read(marker)
    m["setup_isolate"] = [cmd]
    marker.write_text(json.dumps(m))
    capsys.readouterr()
    rc, m = _run_batch(p, marker, monkeypatch)
    assert rc == 0 and m["outcome"] == "pushed", m
    assert _statuses(m) == ["check_failed", "pushed", "pushed"], m
    failed = m["results"][0]
    assert "its setup failed before any test ran" in str(failed), failed
    files = _git_out(origin, "ls-tree", "-r", "--name-only", "proj").split()
    assert "plugins/p/f2.txt" in files and "plugins/p/f3.txt" in files and "plugins/p/f1.txt" not in files
    assert "setup_fault" not in m["checks"], m["checks"]
    assert "isolates the entry that brings it" in capsys.readouterr().out


def test_after_the_setup_fault_alert_a_fault_on_every_head_or_a_pool_still_holds_the_batch(env, monkeypatch):
    """The check alerted on fails in setup on every head, the tip alone too (the host or its pool, not an
    entry): the batch is held setup_fault and blames no entry. No free pool slot holds it at once."""
    from ttp import push, setupfault
    for name, text, kind in (("everywhere", None, "setup"), ("pool", _STARVED, "pool")):
        d = env["tmp"] / name
        d.mkdir()
        argv, ran = _setup_check(d, [text or _head_gone(d)])
        cmd = " ".join(argv)
        if name == "everywhere":
            p, repo, origin, other = _bump_setup(env, monkeypatch, [cmd])
            heads = [_entry(repo, f"e{n}", {f"plugins/p/f{n}.txt": f"{n}\n"}) for n in (1, 2, 3)]
        else:
            p.set_config("delivery.push_checks", [cmd])
        before = _git_out(origin, "rev-parse", "proj")
        marker = _batch_marker(p, heads, bid=name)
        m = push._read(marker)
        m["setup_isolate"] = [cmd]
        marker.write_text(json.dumps(m))
        rc, m = _run_batch(p, marker, monkeypatch)
        assert rc == push.BUSY and m["outcome"] == "setup_fault", (name, m)
        assert _statuses(m) == ["requeued"] * 3 and m["checks"]["setup_fault"]["kind"] == kind, (name, m)
        assert _git_out(origin, "rev-parse", "proj") == before, name
        n = setupfault.RERUNS + 1   # runs of one check that failed in setup
        assert ran() == 1 if kind == "pool" else ran() % n == 0 and ran() >= 2 * n, (name, ran())
