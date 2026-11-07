# Shared check failure tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new shared-checks-failure tests here, not there.
from __future__ import annotations


def _pytest_out(ids, extra_failed=0, passed=40, bars=False):
    """pytest's output: `-q` (the required command) prints its last summary line bare, the default
    verbosity (`bars`) wraps it in '='; the suite's own line may follow it."""
    lines = ["..F.", "=" * 20 + " short test summary info " + "=" * 20]
    lines += [f"FAILED {i} - AssertionError: boom" for i in ids]
    summary = f"{len(ids) + extra_failed} failed, {passed} passed, 2 skipped in 3.21s (0:00:03)"
    lines.append(f"{'=' * 10} {summary} {'=' * 10}" if bars else summary)
    lines.append("ttp-suite: wall 3.2s cpu 9.1s in 3.21s")
    return "\n".join(lines) + "\n"


def _shared_fail_task(p, tmp_path, output, started=1000.0):
    """A code task waiting on its own detached checks, which failed with `output` (check_failure.json)."""
    tid, path = _checks_wait_task(p, tmp_path, DONE_ON_PASS, rc="1")
    run_dir = tmp_path / f"run-{tid}"
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (run_dir / "check_failure.json").write_text(json.dumps({"kind": "check_failed", "head": head, "rc": 1,
                                                            "output": output, "check": "pytest -q"}))
    (run_dir / "checks.pid").write_text(json.dumps({"pid": 1, "ts": started, "worktree": str(path), "head": head,
                                                    "cmd": ["pytest -q"], "fresh": False, "full": False}))
    return tid, run_dir


_IDS = ["tests/test_a.py::test_one", "tests/test_b.py::test_two[x - y]"]


def _seed_other_failures(p, ids, n=2, output=None):
    from ttp import sharedchecks
    sigs = sharedchecks.failure_sigs(_pytest_out(ids) if output is None else output, ids)
    for k in range(n):
        sharedchecks.record(p.db, 9000 + k, f"{k:040x}", ids, time.time() - 600, sigs)


def _shared_daemon(p, monkeypatch):
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "_start_checks", lambda path, out, run_id, tid, argv, arrived=None: started.append((tid, argv)) or True)
    return d, started


def test_failing_ids_reads_pytest_summaries_and_nothing_else():
    from ttp import sharedchecks
    assert sharedchecks.failing_ids(_pytest_out(_IDS)) == (sorted(_IDS), True)
    out = _pytest_out(["tests/test_a.py::test_one"]).replace("FAILED", "ERROR").replace(" failed,", " error,")
    assert sharedchecks.failing_ids(out) == (["tests/test_a.py::test_one"], True)
    assert sharedchecks.failing_ids(_pytest_out(_IDS, bars=True)) == (sorted(_IDS), True)
    # A summary counting more failures than the lines shown (cut output): not complete.
    assert sharedchecks.failing_ids(_pytest_out(_IDS, extra_failed=1))[1] is False
    assert sharedchecks.failing_ids(_pytest_out(_IDS, extra_failed=1, bars=True))[1] is False
    # Another runner's output.
    assert sharedchecks.failing_ids("error[E0308]: mismatched types\nerror: could not compile `x`\n") == ([], False)
    assert sharedchecks.failing_ids("FAILED (failures=2)\nRan 9 tests in 0.1s\n") == ([], False)


def test_failing_ids_reads_real_pytest_q_output(tmp_path):
    from ttp import sharedchecks
    (tmp_path / "test_tiny.py").write_text("def test_ok():\n    pass\n\ndef test_bad():\n    assert 0\n")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_tiny.py"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120,
                       env={k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")})
    assert r.returncode == 1, r.stdout + r.stderr
    assert sharedchecks.failing_ids(r.stdout) == (["test_tiny.py::test_bad"], True), r.stdout


def _pytest_out_with(errors: dict, passed=40):
    """pytest -q output whose FAILURES part shows each id's `E` lines (errors: id -> lines)."""
    lines = ["..F.", "=" * 20 + " FAILURES " + "=" * 20]
    for i, es in errors.items():
        name = i.split("::", 1)[1].replace("::", ".")
        lines += [f"{'_' * 10} {name} {'_' * 10}", "", "    def test():", ">       assert x", *[f"E       {e}" for e in es],
                  "", f"{i.split('::')[0]}:12: AssertionError"]
    lines += ["=" * 20 + " short test summary info " + "=" * 20, *[f"FAILED {i}" for i in errors],
              f"{len(errors)} failed, {passed} passed in 3.21s"]
    return "\n".join(lines) + "\n"


_LINT = "tests/test_r.py::test_every_state_write_is_durable"


def test_failure_signatures_tell_one_cause_from_another(tmp_path):
    from ttp import sharedchecks
    ids = [_LINT, "tests/test_r.py::TestX::test_y[a-1]"]
    def sigs(e0, e1=("assert 0.52 < 0.5",)):
        return sharedchecks.failure_sigs(_pytest_out_with({ids[0]: e0, ids[1]: list(e1)}), ids)
    a = sigs(["AssertionError: write these:", "checkcap.py:504: with open(state / LOCK, 'a') as lf:"])
    b = sigs(["AssertionError: write these:", "activity.py:56: with open(state / LOCK, 'a') as lf:"])
    assert set(a) == set(ids) and a[ids[0]] != b[ids[0]] and a[ids[1]] == b[ids[1]]
    # Line numbers, timings, addresses and temporary paths do not change a signature.
    c = sigs(["AssertionError: write these:", "checkcap.py:517: with open(state / LOCK, 'a') as lf:"],
             ["assert 0.73 < 0.5"])
    assert c == a
    d = sigs(["FileNotFoundError: /tmp/pytest-of-u/pytest-7/test_x0/a.json at 0x7f3a"])
    e = sigs(["FileNotFoundError: /tmp/pytest-of-u/pytest-9/test_x1/a.json at 0x7f9b"])
    assert d[ids[0]] == e[ids[0]]
    # Cut output (no FAILURES part): the summary line's message; neither it nor `E` lines: no signature.
    cut = sharedchecks.failure_sigs(_pytest_out(ids), ids)
    assert cut[ids[0]] and cut[ids[0]] == cut[ids[1]] != a[ids[0]]
    assert sharedchecks.failure_sigs(_pytest_out_with({ids[0]: []}), ids)[ids[0]] == ""


def test_failure_signatures_read_real_pytest_q_output(tmp_path):
    from ttp import sharedchecks
    outs = []
    for k, why in enumerate(["checkcap.py", "activity.py", "checkcap.py"]):
        (run := tmp_path / f"r{k}").mkdir()   # one dir each: a same-size rewrite would reuse stale bytecode
        (run / "test_tiny.py").write_text(
            "import pytest\n\nclass TestX:\n    @pytest.mark.parametrize('n', [1])\n"
            f"    def test_bad(self, n):\n        assert not ['{why}:{k}: open(x, \"a\")']\n")
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_tiny.py"],
                           cwd=run, capture_output=True, text=True, timeout=120,
                           env={k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")})
        assert r.returncode == 1, r.stdout + r.stderr
        ids, complete = sharedchecks.failing_ids(r.stdout)
        assert ids == ["test_tiny.py::TestX::test_bad[1]"] and complete, r.stdout
        outs.append(sharedchecks.failure_sigs(r.stdout, ids).get(ids[0]))
    assert outs[0] and outs[1] and outs[0] != outs[1] and outs[0] == outs[2]


def test_one_id_failing_elsewhere_for_another_reason_is_not_shared(env):
    """The same lint test failed in other changes on lines those changes added: each change's own
    failure, so the task wakes, not held. The same cause elsewhere is shared; a row without signatures
    counts for no signed failure."""
    from ttp import sharedchecks
    p = make(env)
    now = time.time()
    other = lambda line: sharedchecks.failure_sigs(_pytest_out_with({_LINT: ["write these:", line]}), [_LINT])
    for k, line in enumerate(["activity.py:56: os.replace(log, OLD)", "lease.py:341: tmp.write_text(x)"]):
        sharedchecks.record(p.db, 9000 + k, f"{k:040x}", [_LINT], now - 600, other(line))
    mine = other("checkcap.py:512: os.replace(log, WAITS_OLD)")
    assert not sharedchecks.shared(p.db, 9100, f"{9:040x}", [_LINT], now, {}, sigs=mine)
    assert sharedchecks.shared(p.db, 9100, f"{9:040x}", [_LINT], now, {})   # no signature: by id as before
    sharedchecks.record(p.db, 9002, f"{2:040x}", [_LINT], now - 600, other("checkcap.py:9: os.replace(log, WAITS_OLD)"))
    sharedchecks.record(p.db, 9003, f"{3:040x}", [_LINT], now - 600)   # an older row without signatures
    assert not sharedchecks.shared(p.db, 9100, f"{9:040x}", [_LINT], now, {}, sigs=mine)
    sharedchecks.record(p.db, 9004, f"{4:040x}", [_LINT], now - 600, other("checkcap.py:77: os.replace(log, WAITS_OLD)"))
    assert sharedchecks.shared(p.db, 9100, f"{9:040x}", [_LINT], now, {}, sigs=mine)


def test_a_daemon_task_failing_a_shared_id_for_its_own_reason_wakes(env, monkeypatch, tmp_path):
    from ttp import sharedchecks
    p = make(env)
    d, _ = _shared_daemon(p, monkeypatch)
    out = lambda line: _pytest_out_with({_LINT: ["write these:", line]})
    for k in range(2):
        o = out(f"activity.py:{k}: os.replace(log, OLD)")
        sharedchecks.record(p.db, 9000 + k, f"{k:040x}", [_LINT], time.time() - 600, sharedchecks.failure_sigs(o, [_LINT]))
    tid, _ = _shared_fail_task(p, tmp_path, out("checkcap.py:512: os.replace(log, WAITS_OLD)"))
    d._probe_passed(p.db.task(tid), time.time())
    res = json.loads(p.db.task(tid)["result"])
    assert res.get("escalated_wake") and "shared_hold" not in res and _ready(p, tid)
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Fix shared check failure%'")
    (row,) = p.db.q("SELECT sigs FROM checks_failures WHERE task=?", (tid,))
    assert set(json.loads(row["sigs"])) == {_LINT}
    tid2, _ = _shared_fail_task(p, tmp_path, out("activity.py:7: os.replace(log, OLD)"))   # the same cause: held
    d._probe_passed(p.db.task(tid2), time.time())
    assert "shared_hold" in json.loads(p.db.task(tid2)["result"])

def test_a_shared_checks_failure_holds_tasks_in_one_incident_with_one_fix_task(env, monkeypatch, tmp_path):
    from ttp import audit, sharedchecks
    p = make(env)
    d, started = _shared_daemon(p, monkeypatch)
    _seed_other_failures(p, _IDS)
    tids = [_shared_fail_task(p, tmp_path, _pytest_out(_IDS))[0] for _ in range(2)]
    now = time.time()
    for tid in tids:
        d._probe_passed(p.db.task(tid), now)
    d._probe_passed(p.db.task(tids[0]), now)   # the same failure again: still held, not counted twice
    for tid in tids:
        task = p.db.task(tid)
        res = json.loads(task["result"])
        assert "woke" not in res and not res.get("escalated_wake"), res
        assert task["not_before"] > now + 3600 and not _ready(p, tid)
        assert res["shared_hold"]["ids"] == sorted(_IDS) and res["shared_hold"]["reruns"] == 0
    (inc,) = p.db.q("SELECT * FROM checks_incidents")
    assert sorted(json.loads(inc["tasks"])) == tids and inc["held"] == 2 and inc["closed"] is None
    (fix,) = p.db.q("SELECT * FROM tasks WHERE title LIKE 'Fix shared check failure%'")
    assert fix["id"] == inc["fix_task"] and fix["kind"] == "code"
    assert all(i in fix["spec"] for i in _IDS) and all(f"#{t}" in fix["spec"] for t in tids)
    assert len(p.db.q("SELECT id FROM events WHERE kind='checks_shared'")) == 1
    assert not started
    assert len(p.db.q("SELECT id FROM runs WHERE task IN (?,?)", tids)) == 2   # no model run
    line = sharedchecks.audit_line(p.db, now - 60)
    assert "1 incident(s), 2 task(s) held, 2 wakes avoided" in line, line
    assert line in audit.lines(p.db, now + 1)


def test_failures_of_one_changes_own_earlier_tasks_are_not_shared(env):
    """A code task, its review fix and its `continue:` task fail the same id on successive heads:
    that is the change's own regression, so it is not shared; two tasks of other lineages make it so."""
    from ttp import sharedchecks
    p = make(env)
    now = time.time()
    code = p.db.add_task("code: change", "spec", kind="code")
    fix = p.db.add_task("Fix review #1: change", "spec", kind="code", labels=[f"continues:{code}"])
    cont = p.db.add_task("continue: change", "spec", kind="code", labels=[f"continues:{fix}"])
    review = p.db.add_task(f"Review #{code}: change", "spec", kind="review", labels=[f"auto_review:{code}"])
    rereview = p.db.add_task(f"Re-review #{review}: change", "spec", kind="review", labels=[f"auto_review:{fix}"])
    for k, t in enumerate((code, review, fix, rereview)):
        sharedchecks.record(p.db, t, f"{k // 2 + 1:040x}", _IDS, now - 600)
    assert len({r["root"] for r in p.db.q("SELECT root FROM checks_failures")}) == 1
    via_review = p.db.add_task("Fix review: change", "spec", kind="code", labels=[f"continues:{rereview}"])
    assert sharedchecks.root(p.db, via_review) == code   # a hop through a review keeps the lineage
    assert not sharedchecks.shared(p.db, cont, f"{9:040x}", _IDS, now, {})
    for k in range(2):
        other = p.db.add_task(f"code: other {k}", "spec", kind="code")
        sharedchecks.record(p.db, other, f"{k + 5:040x}", _IDS, now - 600)
    assert sharedchecks.shared(p.db, cont, f"{9:040x}", _IDS, now, {})


def test_shared_checks_count_other_lineages_not_tasks(env):
    """Two tasks of one other change (its code task and its review fix, on successive heads) are one
    lineage: not enough for shared_min_tasks=2; two other changes are."""
    from ttp import sharedchecks
    p = make(env)
    now = time.time()
    mine = p.db.add_task("code: mine", "spec", kind="code")
    other = p.db.add_task("code: other", "spec", kind="code")
    ofix = p.db.add_task("Fix review #1: other", "spec", kind="code", labels=[f"continues:{other}"])
    for k, t in enumerate((other, ofix)):
        sharedchecks.record(p.db, t, f"{k + 1:040x}", _IDS, now - 600)
    assert not sharedchecks.shared(p.db, mine, f"{9:040x}", _IDS, now, {})
    third = p.db.add_task("code: third", "spec", kind="code")
    sharedchecks.record(p.db, third, f"{5:040x}", _IDS, now - 600)
    assert sharedchecks.shared(p.db, mine, f"{9:040x}", _IDS, now, {})


def test_the_known_bad_push_tip_makes_a_failure_shared(env, monkeypatch, tmp_path):
    from ttp import pushq
    p = make(env)
    d, _ = _shared_daemon(p, monkeypatch)
    tid, _ = _shared_fail_task(p, tmp_path, _pytest_out(_IDS[:1]))
    p.db.set_kv(pushq.KV, {"bad_tip": {"tip": "f" * 40, "fails": pushq.TIP_FAILS, "ids": _IDS[:1]}})
    d._probe_passed(p.db.task(tid), time.time())
    assert "shared_hold" in json.loads(p.db.task(tid)["result"])


@pytest.mark.parametrize("output", [
    _pytest_out(_IDS + ["tests/test_c.py::test_new"]),        # one id is not shared: mixed
    "error[E0308]: mismatched types\nerror: could not compile `x`\n",   # another runner
    _pytest_out(_IDS, extra_failed=1)])                           # the list is not complete
def test_a_checks_failure_not_fully_shared_wakes_the_task_as_before(env, monkeypatch, tmp_path, output):
    p = make(env)
    d, _ = _shared_daemon(p, monkeypatch)
    _seed_other_failures(p, _IDS)
    tid, _ = _shared_fail_task(p, tmp_path, output)
    d._probe_passed(p.db.task(tid), time.time())
    res = json.loads(p.db.task(tid)["result"])
    assert res.get("escalated_wake") and "its checks failed" in res["woke"] and "shared_hold" not in res
    assert _ready(p, tid)
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Fix shared check failure%'")


_GUARD = "tests/test_r.py::test_every_write_is_durable"


def _guard_out(*loose):
    """A whole-tree guard test's failure as `ttp checks` keeps it: its traceback names the
    change's own offending lines."""
    lines = ["...F.", "=" * 20 + " FAILURES " + "=" * 20, "_" * 10 + " test_every_write_is_durable " + "_" * 10, "",
             "    def test_every_write_is_durable():", ">       assert not loose, 'write these durably'",
             "E       AssertionError: write these durably"]
    lines += [f"E         {x}" for x in loose]
    lines += [f"E       assert not {list(loose)!r}", "", "tests/test_r.py:25007: AssertionError",
              "=" * 20 + " short test summary info " + "=" * 20, f"FAILED {_GUARD}",
              "1 failed, 3924 passed, 6 skipped in 400.39s (0:06:40)"]
    return "\n".join(lines) + "\n"


def test_a_guard_test_each_change_fails_on_its_own_code_is_not_shared(env, monkeypatch, tmp_path):
    # Two other changes failed the same source-wide guard, each on its own new write: the id
    # matches, the failure does not, so the task wakes to fix its own code and no fix task opens.
    from ttp import sharedchecks
    p = make(env)
    d, _ = _shared_daemon(p, monkeypatch)
    now = time.time()
    for k, line in enumerate(['lease.py:88: open(dst, "wb")', 'replica.py:301: open(out, "wb")']):
        sharedchecks.record(p.db, 9000 + k, f"{k:040x}", [_GUARD], now - 600,
                            sharedchecks.failure_sigs(_guard_out(line), [_GUARD]))
    tid, _ = _shared_fail_task(p, tmp_path, _guard_out('heal.py:690: (d / "detached.json").write_text(x)'))
    d._probe_passed(p.db.task(tid), now)
    res = json.loads(p.db.task(tid)["result"])
    assert "shared_hold" not in res and res.get("escalated_wake") and _ready(p, tid)
    assert not p.db.q("SELECT id FROM tasks WHERE title LIKE 'Fix shared check failure%'")


def test_the_same_failure_on_other_heads_is_shared_whatever_its_numbers(env, monkeypatch, tmp_path):
    from ttp import sharedchecks
    p = make(env)
    d, _ = _shared_daemon(p, monkeypatch)
    _seed_other_failures(p, [_GUARD], output=_guard_out("heal.py:690: x.write_text(y)"))
    tid, _ = _shared_fail_task(p, tmp_path, _guard_out("heal.py:702: x.write_text(y)"))
    d._probe_passed(p.db.task(tid), time.time())
    assert "shared_hold" in json.loads(p.db.task(tid)["result"])
    # A row recorded before signatures (none stored) counts for no signed failure.
    sharedchecks.record(p.db, 9100, "e" * 40, ["tests/test_q.py::test_x"], time.time() - 600)
    assert not sharedchecks.shared(p.db, tid, "d" * 40, ["tests/test_q.py::test_x"], time.time(), {"checks": {
        "shared_min_tasks": 1}}, sigs={"tests/test_q.py::test_x": ""})


def test_failure_sigs_read_real_pytest_tracebacks(tmp_path):
    from ttp import sharedchecks
    (tmp_path / "test_g.py").write_text("import os\nclass TestK:\n    def test_bad(self):\n"
                                        "        assert os.environ.get('W', 'a.py:1') == '', 'loose'\n")
    def run(w):
        return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_g.py"],
                              cwd=tmp_path, capture_output=True, text=True, timeout=120,
                              env={**{k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}, "W": w}).stdout
    i = ["test_g.py::TestK::test_bad"]
    a, b, c = (sharedchecks.failure_sigs(run(w), i)[i[0]] for w in ("a.py:12", "a.py:345", "b.py:12"))
    assert a and a == b != c


def test_a_held_task_reruns_its_checks_once_the_tip_moves_past_its_tests_or_the_backoff(env, monkeypatch, tmp_path):
    from ttp import pushq, sharedchecks
    p = make(env)
    d, started = _shared_daemon(p, monkeypatch)
    _seed_other_failures(p, _IDS)
    tid, run_dir = _shared_fail_task(p, tmp_path, _pytest_out(_IDS))
    tips, touched = ["t1"], []
    monkeypatch.setattr(d, "_push_tip", lambda: tips[0])
    monkeypatch.setattr(pushq, "touches", lambda p_, old, new, files: touched.append((old, new, files)) or new == "t3")
    now = time.time()
    d._probe_passed(p.db.task(tid), now)
    d._probe_passed(p.db.task(tid), now + 60)           # tip unchanged: sleeps on
    tips[0] = "t2"
    d._probe_passed(p.db.task(tid), now + 120)          # tip moved, but not past the failing tests
    assert touched == [("t1", "t2", ["tests/test_a.py", "tests/test_b.py"])] and not started
    tips[0] = "t3"
    d._probe_passed(p.db.task(tid), now + 180)          # tip moved past a commit touching them
    assert len(started) == 1 and started[0][0] == tid and started[0][1] == ["pytest -q"]
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert "woke" not in res and res["shared_hold"]["reruns"] == 1 and task["not_before"] > now + 3600
    # The rerun failed the same way (a new checks.pid): held again; then the backoff alone reruns it.
    pid = json.loads((run_dir / "checks.pid").read_text())
    (run_dir / "checks.pid").write_text(json.dumps({**pid, "ts": 2000.0}))
    d._probe_passed(p.db.task(tid), now + 240)
    hold = json.loads(p.db.task(tid)["result"])["shared_hold"]
    assert hold["started"] == 2000.0 and hold["tip"] == "t3"
    d._probe_passed(p.db.task(tid), hold["until"] + 1)
    assert len(started) == 2
    # Past RERUNS_MAX reruns the next failure wakes the task at its tier.
    res = json.loads(p.db.task(tid)["result"])
    p.db.update_task(tid, result=json.dumps({**res, "shared_hold": {**res["shared_hold"],
                                                                     "reruns": sharedchecks.RERUNS_MAX}}))
    (run_dir / "checks.pid").write_text(json.dumps({**pid, "ts": 3000.0}))
    d._probe_passed(p.db.task(tid), now + 300)
    res = json.loads(p.db.task(tid)["result"])
    assert res.get("escalated_wake") and "its checks failed" in res["woke"]
    assert p.db.task(tid)["not_before"] <= now + 300   # due at the wake's own time


def test_touches_finds_commits_changing_the_failing_test_files(env):
    from ttp import pushq
    p = make(env)
    git = ["git", "-C", str(p.root)]
    old = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (p.root / "tests").mkdir(exist_ok=True)
    (p.root / "tests" / "test_a.py").write_text("def test_one(): pass\n")
    subprocess.run([*git, "add", "tests/test_a.py"], check=True, capture_output=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fix test"], check=True,
                   capture_output=True)
    new = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert pushq.touches(p, old, new, ["tests/test_a.py"])
    assert not pushq.touches(p, old, new, ["tests/test_b.py"])
    assert pushq.touches(p, "0" * 40, new, ["tests/test_b.py"])   # git cannot tell: rerun


def test_a_review_failed_only_by_batch_push_checks_is_check_only_and_routine_once_owned(env, monkeypatch):
    """A recorded batch failure: the push queue fails the review with review_failure check_only and
    the failing test ids, so the review-area cap skips it; the coordinator's effort rules keep it
    routine only while a shared-check incident or a live fix task owns every failing test."""
    from ttp import coordinator as coord, reviewcap, sharedchecks
    s = _pq(env, monkeypatch)
    _pq_plan(s, outcome="nothing", row="check_failed", detail={"cmd": "pytest -q", "tail": _pytest_out(_IDS)})
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_batch(s)
    _pq_tend(s)
    db, cfg = s.p.db, s.p.config()
    t = db.task(s.review)
    res = json.loads(t["result"])
    assert t["status"] == "failed" and res["review_failure"] == "check_only" and res["push_failed"]["cmd"] == "pytest -q"
    assert res[sharedchecks.FAILING_KEY] == sorted(_IDS) and res[sharedchecks.COMPLETE_KEY] is True
    assert s.review not in reviewcap.failed_reviews(db, 0) and reviewcap.area(s.p, cfg, t) is None
    [ev] = [e["id"] for e in s.p.db.q("SELECT id FROM events WHERE id>? AND kind='task_failed' AND status='queued'",
                                       (mark,))]
    db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    db.x("DELETE FROM messages WHERE direction='out'")

    def trig():
        low: list = []
        return coord.effort_triggers(db, cfg, [ev], None, [], None, time.time(), None, low)[0], low
    assert trig()[0] == ["failed review"], "nothing owns the failing tests yet"
    # A live fix task naming one test is not enough; naming both is.
    fix = db.add_task("Fix flaky test", f"fix {_IDS[0]}", kind="code", origin="coordinator")
    assert trig()[0] == ["failed review"]
    db.update_task(fix, spec=f"fix {_IDS[0]} and {_IDS[1]}")
    out, low = trig()
    assert out == [] and low == [f"{coord.LOWERED_CHECK_ONLY}: event {ev} (2 failing test(s) owned by #{fix})"], low
    db.update_task(fix, status="done")
    assert trig()[0] == ["failed review"], "a finished fix task owns nothing"
    # An open shared-check incident owns them while its fix task is live.
    iid, opened = sharedchecks.incident(db, {"id": s.code}, list(_IDS), "boom", time.time(), cfg)
    out, low = trig()
    fid = db.one("SELECT fix_task FROM checks_incidents WHERE id=?", (iid,))["fix_task"]
    assert opened and out == [] and f"owned by incident {iid} (fix #{fid})" in low[0], low
    # Ids that may not be all of them (no complete pytest summary) still raise the turn.
    db.update_task(s.review, result=json.dumps({**res, sharedchecks.COMPLETE_KEY: False}))
    assert trig()[0] == ["failed review"]


def test_a_batch_failure_without_pytest_ids_is_check_only_but_still_raises_the_turn(env, monkeypatch):
    from ttp import coordinator as coord, sharedchecks
    s = _pq(env, monkeypatch)
    _pq_plan(s, outcome="nothing", row="check_failed", detail={"cmd": "make lint", "tail": "lint: 3 errors"})
    _pq_hand_off(env, s)
    mark = _pq_mark(s.p)
    _pq_batch(s)
    _pq_tend(s)
    res = json.loads(s.p.db.task(s.review)["result"])
    assert res["review_failure"] == "check_only" and res[sharedchecks.FAILING_KEY] == []
    [ev] = [e["id"] for e in s.p.db.q("SELECT id FROM events WHERE id>? AND kind='task_failed' AND status='queued'",
                                       (mark,))]
    assert coord.effort_triggers(s.p.db, s.p.config(), [ev], None, [], None, time.time(), None, [])[0] == \
        ["failed review"]


def test_a_repeat_shared_failure_gets_a_fix_task_that_must_prove_stability(env):
    from ttp import sharedchecks
    p = make(env)
    now = 1_000_000.0
    t1, t2, t3 = (p.db.add_task(f"held {k}", "x", kind="code") for k in range(3))
    iid1, opened = sharedchecks.incident(p.db, p.db.task(t1), _IDS[:1], "boom", now, {})
    assert opened
    (fix1,) = p.db.q("SELECT fix_task FROM checks_incidents WHERE id=?", (iid1,))
    first = p.db.task(fix1["fix_task"])
    assert first["tier"] == "standard" and "Repeat incident" not in first["spec"]
    p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (now + 3600, first["id"]))
    _land(p, first["id"], now + 7200)
    # The same test fails shared again two days later: the fix did not hold.
    later = now + 2 * 86400
    iid2, opened = sharedchecks.incident(p.db, p.db.task(t2), _IDS, "boom again", later, {})
    assert opened and iid2 != iid1
    inc2 = p.db.one("SELECT fix_task FROM checks_incidents WHERE id=?", (iid2,))
    fix = p.db.task(inc2["fix_task"])
    assert fix["tier"] == "standard" and fix["kind"] == "code"
    spec = fix["spec"]
    assert "Repeat incident" in spec and f"- {_IDS[0]}" in spec.split("Repeat incident", 1)[1]
    assert _IDS[1] not in spec.split("Repeat incident", 1)[1]   # only the id fixed before repeats
    assert "root cause" in spec and "fake clock" in spec
    assert f"{sharedchecks.REPEAT_RUNS} times under parallel load" in spec and "TTP_TEST_JOBS" in spec
    # Extending the queued fix task keeps the stricter spec.
    sharedchecks.incident(p.db, p.db.task(t3), _IDS[1:], "boom", later + 60, {})
    spec = p.db.task(fix["id"])["spec"]
    assert "Repeat incident" in spec and f"#{t3}" in spec
    # A fix that ended longer ago than the repeat window does not count, even when the incident was
    # only marked closed later.
    p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (later, fix["id"]))
    _land(p, fix["id"], later)
    much_later = later + 4 * 86400
    iid3, opened = sharedchecks.incident(p.db, p.db.task(t1), _IDS, "boom", much_later, {})
    assert opened
    fix3 = p.db.task(p.db.one("SELECT fix_task FROM checks_incidents WHERE id=?", (iid3,))["fix_task"])
    assert "Repeat incident" not in fix3["spec"]
    assert sharedchecks.repeats(p.db, _IDS, much_later, {"checks": {"shared_repeat_h": 200}}) == sorted(_IDS)


def _land(p, tid, ts, status="pushed", task=None):
    """A push queue row recording that `tid`'s change landed at `ts` (pushed by `task`, else itself)."""
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,pushed_sha) "
           "VALUES(?,?,?,?,?,?,?,?)", (task or tid, f"ttp/t{tid}", "a" * 40, "main", status, ts, ts, "b" * 40))


def _second_spec(p, fix_status, landed, now=1_000_000.0):
    """The spec of the fix task for a shared failure on the same id one day after an earlier
    incident whose fix task ended `fix_status` (landed or not)."""
    from ttp import sharedchecks
    t1, t2 = (p.db.add_task(f"held {k}", "x", kind="code") for k in range(2))
    iid1, _ = sharedchecks.incident(p.db, p.db.task(t1), _IDS[:1], "boom", now, {})
    fix1 = p.db.one("SELECT fix_task FROM checks_incidents WHERE id=?", (iid1,))["fix_task"]
    p.db.x("UPDATE tasks SET status=?, updated=? WHERE id=?", (fix_status, now + 3600, fix1))
    if landed:
        _land(p, fix1, now + 7200)
    iid2, opened = sharedchecks.incident(p.db, p.db.task(t2), _IDS[:1], "boom", now + 86400, {})
    assert opened and iid2 != iid1
    return p.db.task(p.db.one("SELECT fix_task FROM checks_incidents WHERE id=?", (iid2,))["fix_task"])["spec"]


def test_a_shared_failure_after_a_fix_that_did_not_land_is_no_repeat(env):
    # The fix task handed off done but its change is not on the push branch yet: held tasks that
    # rerun on the old tip and fail again prove nothing about the fix.
    spec = _second_spec(make(env), "done", landed=False)
    assert "Repeat incident" not in spec and "times under parallel load" not in spec


def test_a_shared_failure_after_a_landed_fix_is_a_repeat(env):
    spec = _second_spec(make(env), "done", landed=True)
    assert "Repeat incident" in spec and f"- {_IDS[0]}" in spec.split("Repeat incident", 1)[1]


def test_a_shared_failure_after_a_landed_review_of_the_fix_is_a_repeat(env):
    from ttp import sharedchecks
    p = make(env)
    now = 1_000_000.0
    t1, t2 = (p.db.add_task(f"held {k}", "x", kind="code") for k in range(2))
    iid1, _ = sharedchecks.incident(p.db, p.db.task(t1), _IDS[:1], "boom", now, {})
    fix1 = p.db.one("SELECT fix_task FROM checks_incidents WHERE id=?", (iid1,))["fix_task"]
    p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (now - 10 * 86400, fix1))
    rev = p.db.add_task(f"Review #{fix1}", "x", kind="review", labels=[f"auto_review:{fix1}"])
    # Landed by its review one hour ago: the window runs from the landing, not the old hand-off.
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,pushed_sha) "
           "VALUES(?,?,?,?,?,?,?,?)", (rev, "ttp/other", "a" * 40, "main", "pushed", now, now, "b" * 40))
    iid2, _ = sharedchecks.incident(p.db, p.db.task(t2), _IDS[:1], "boom", now + 3600, {})
    fix2 = p.db.one("SELECT fix_task FROM checks_incidents WHERE id=?", (iid2,))["fix_task"]
    assert "Repeat incident" in p.db.task(fix2)["spec"]


def test_a_shared_failure_after_a_failed_or_cancelled_fix_is_no_repeat(env):
    for status in ("failed", "cancelled"):
        spec = _second_spec(make(env), status, landed=False)
        assert "Repeat incident" not in spec, status
    # Even with a push row (a stale or partial record), a fix that did not end done makes no repeat.
    spec = _second_spec(make(env), "failed", landed=True)
    assert "Repeat incident" not in spec


def test_a_shared_failure_after_a_missing_fix_task_is_no_repeat(env):
    from ttp import sharedchecks
    p = make(env)
    now = 1_000_000.0
    t1 = p.db.add_task("held", "x", kind="code")
    sharedchecks.ensure(p.db)
    p.db.x("INSERT INTO checks_incidents(ids,opened,last,fix_task,tasks,closed) VALUES(?,?,?,?,?,?)",
           (json.dumps(_IDS[:1]), now, now, 999_999, json.dumps([t1]), now + 60))
    assert sharedchecks.repeats(p.db, _IDS, now + 3600, {}) == []


def test_covered_matches_test_ids_on_identifier_boundaries(env):
    from ttp import sharedchecks
    p = make(env)
    p.db.add_task("code: fix", "Fix tests/x.py::test_ab and tests/y.py::test_p[1]", kind="code")
    assert sharedchecks.covered(p.db, ["tests/x.py::test_ab"])
    assert sharedchecks.covered(p.db, ["tests/x.py::test_ab"]) == sharedchecks.covered(p.db, ["tests/y.py::test_p[1]"])
    assert not sharedchecks.covered(p.db, ["tests/x.py::test_a"])      # prefix of another id
    assert not sharedchecks.covered(p.db, ["tests/y.py::test_p"])      # prefix of a parametrized id
    p.db.add_task("code: more", "Also tests/x.py::test_a.", kind="code")
    assert sharedchecks.covered(p.db, ["tests/x.py::test_a"])          # punctuation ends the id


def _tip_daemon(p, monkeypatch, tip="c" * 40, holds_tip=False):
    """_shared_daemon on a push-branch tip `tip`, recording the reruns of failing ids there instead of
    starting them; `holds_tip`: the failing heads already contain the tip."""
    from ttp import worktree
    d, started = _shared_daemon(p, monkeypatch)
    tips = []
    monkeypatch.setattr(d, "_push_tip", lambda: tip)
    monkeypatch.setattr(worktree, "is_ancestor", lambda p_, older, newer: holds_tip)
    monkeypatch.setattr(d, "_start_tip_check", lambda t, ids, key: tips.append((t, ids, key)) or True)
    return d, started, tips


def _tip_ran(p, d, tip, ids, rc, now):
    """A rerun of `ids` on `tip` that already ended with exit code `rc`."""
    from ttp import sharedchecks
    key = sharedchecks.tip_key(tip, ids)
    sharedchecks.tip_started(p.db, key, tip, ids, now)
    d._tip_dir(key).mkdir(parents=True, exist_ok=True)
    (d._tip_dir(key) / "rc").write_text(rc + "\n")
    return key


def _fixes(p):
    return p.db.q("SELECT * FROM tasks WHERE title LIKE 'Fix shared check failure%' ORDER BY id")


def test_a_second_shared_failure_while_its_fix_is_in_review_or_pushing_links_to_that_fix(env, monkeypatch, tmp_path):
    from ttp import sharedchecks
    p = make(env)
    d, _, tips = _tip_daemon(p, monkeypatch, holds_tip=True)
    _seed_other_failures(p, _IDS)
    t1 = _shared_fail_task(p, tmp_path, _pytest_out(_IDS))[0]
    now = time.time()
    _tip_ran(p, d, "c" * 40, _IDS, "1", now)   # they fail on the tip too: the first failure opens the incident
    d._probe_passed(p.db.task(t1), now)
    (fix,) = _fixes(p)
    # The fix ended done and its review is open: the next failure of the same ids is that fix's.
    p.db.update_task(fix["id"], status="done")
    rev = p.db.add_task(f"Review #{fix['id']}", "x", kind="review", labels=[f"auto_review:{fix['id']}"])
    t2 = _shared_fail_task(p, tmp_path, _pytest_out(_IDS))[0]
    d._probe_passed(p.db.task(t2), now + 60)
    # The review passed and the change waits in the push queue: still that fix's.
    p.db.update_task(rev, status="done")
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
           (rev, f"ttp/t{fix['id']}", "a" * 40, "main", "approved", now, now))
    t3 = _shared_fail_task(p, tmp_path, _pytest_out(_IDS[:1]))[0]
    d._probe_passed(p.db.task(t3), now + 120)
    assert [f["id"] for f in _fixes(p)] == [fix["id"]] and not tips
    (inc,) = p.db.q("SELECT * FROM checks_incidents")
    assert inc["closed"] is None and inc["fix_task"] == fix["id"] and json.loads(inc["tasks"]) == [t1, t2, t3]
    for tid in (t2, t3):
        res = json.loads(p.db.task(tid)["result"])
        assert res["shared_hold"]["incident"] == inc["id"] and "woke" not in res
    assert sharedchecks.covered(p.db, _IDS) == f"incident {inc['id']} (fix #{fix['id']})"
    # Once it landed it owns nothing more: the next failure goes to the tip (here: one the head holds).
    p.db.x("UPDATE push_queue SET status='pushed', pushed_sha=? WHERE task=?", ("b" * 40, rev))
    assert sharedchecks.owner(p.db, _IDS, now + 180) is None
    # A done fix with no review and nothing queued, long after its hand-off, owns nothing either.
    p.db.x("DELETE FROM push_queue")
    p.db.update_task(rev, status="cancelled")
    assert sharedchecks.in_flight(p.db, p.db.task(fix["id"]), now) is False


def test_a_shared_failure_passing_on_the_tip_adds_no_fix_task_and_wakes_its_tasks_to_rebase(env, monkeypatch, tmp_path):
    from ttp import sharedchecks
    p = make(env)
    tip = "c" * 40
    d, started, tips = _tip_daemon(p, monkeypatch, tip=tip)
    _seed_other_failures(p, _IDS)
    t1, t2 = (_shared_fail_task(p, tmp_path, _pytest_out(_IDS))[0] for _ in range(2))
    now = time.time()
    d._probe_passed(p.db.task(t1), now)
    key = sharedchecks.tip_key(tip, _IDS)
    assert tips == [(tip, sorted(_IDS), key)]
    for at in (now, now + 60):   # held without a model run while the tip's rerun runs
        d._probe_passed(p.db.task(t1), at)
        d._probe_passed(p.db.task(t2), at)
    assert len(tips) == 1 and not _fixes(p) and not p.db.q("SELECT id FROM checks_incidents")
    for tid in (t1, t2):
        res = json.loads(p.db.task(tid)["result"])
        assert res["shared_hold"]["tip_check"] == key and "woke" not in res and not _ready(p, tid)
    (d._tip_dir(key)).mkdir(parents=True, exist_ok=True)
    (d._tip_dir(key) / "rc").write_text("0\n")
    d._probe_passed(p.db.task(t1), now + 120)
    d._probe_passed(p.db.task(t2), now + 120)
    assert len(tips) == 1 and not _fixes(p) and not p.db.q("SELECT id FROM checks_incidents") and not started
    for tid in (t1, t2):
        task = p.db.task(tid)
        res = json.loads(task["result"])
        assert "pass on the push-branch tip" in res["woke"] and "rebase" in res["woke"], res
        assert res["wake_tier"] == "standard" and task["not_before"] <= now + 120


def _tip_commit(path, name, text):
    """A commit on the parent of `path`'s HEAD setting file `name` to `text`, made without moving the
    worktree (a push-branch tip another change landed): its sha."""
    def run(*a, **k):
        return subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True,
                              **k).stdout.strip()
    base = run("rev-parse", "HEAD~1")
    blob = run("hash-object", "-w", "--stdin", input=text)
    idx = {**os.environ, "GIT_INDEX_FILE": str(path.parent / f"{path.name}.tip-index")}
    run("read-tree", base, env=idx)
    run("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env=idx)
    tree = run("write-tree", env=idx)
    return run("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-p", base, "-m", "tip")


@pytest.mark.parametrize("case", ["rebased", "dirty", "remote", "conflict"])
def test_a_shared_failure_passing_on_the_tip_rebases_a_clean_unpublished_worktree_and_reruns_its_checks(
        env, monkeypatch, tmp_path, case):
    # The daemon rebases the task's clean worktree onto the tip its failing ids pass on and starts its
    # checks again on the new head, without a model run. A dirty worktree, a head on a remote ref (a
    # force push to republish) or a rebase conflict (aborted) leave the worktree as it was and wake the
    # task as before.
    p = make(env)
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    _seed_other_failures(p, _IDS)
    tid, run_dir = _shared_fail_task(p, tmp_path, _pytest_out(_IDS))
    path = Path(json.loads((run_dir / "checks.pid").read_text())["worktree"])
    git = ["git", "-C", str(path)]

    def rev(ref="HEAD"):
        return subprocess.run([*git, "rev-parse", ref], capture_output=True, text=True).stdout.strip()

    head = rev()
    tip = _tip_commit(path, "fix.txt" if case == "conflict" else "other.txt", "theirs\n")
    d, started, tips = _tip_daemon(p, monkeypatch, tip=tip)
    if case == "dirty":
        (path / "fix.txt").write_text("edited\n")
    if case == "remote":
        subprocess.run([*git, "update-ref", "refs/remotes/origin/published", head], check=True)
    now = time.time()
    _tip_ran(p, d, tip, _IDS, "0", now)
    d._probe_passed(p.db.task(tid), now)
    res, new = json.loads(p.db.task(tid)["result"]), rev()
    status = subprocess.run([*git, "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                            text=True).stdout
    assert not _fixes(p) and not tips
    if case == "rebased":
        assert new != head and subprocess.run([*git, "merge-base", "--is-ancestor", tip, new]).returncode == 0
        assert (path / "other.txt").read_text() == "theirs\n" and not status
        assert started == [(tid, ["pytest -q"])] and "woke" not in res and "shared_hold" not in res
        assert not _ready(p, tid)
        return
    assert new == head and not started, (new, head, started)
    assert "rebase" in res["woke"] and "the daemon did not rebase it" in res["woke"], res
    want = {"dirty": "uncommitted changes", "remote": "refs/remotes/origin/published",
            "conflict": "stopped and was aborted"}[case]
    assert want in res["woke"], res["woke"]
    assert bool(status) == (case == "dirty")
    if case == "conflict":   # aborted: no rebase left in progress, the worktree's file as it was
        gd = Path(rev("--absolute-git-dir"))
        assert not (gd / "rebase-merge").exists() and not (gd / "rebase-apply").exists()
        assert (path / "fix.txt").read_text() == "fixed\n"


@pytest.mark.parametrize("rc", ["1", "124", None])
def test_a_shared_failure_not_passing_on_the_tip_adds_exactly_one_fix_task(env, monkeypatch, tmp_path, rc):
    # Failing there (1), timing out (124) or the rerun never ending (None, past its bound) all leave the
    # failure as it was: one incident with one fix task, the later tasks linked to it.
    from ttp import sharedchecks
    p = make(env)
    tip = "c" * 40
    d, _, tips = _tip_daemon(p, monkeypatch, tip=tip)
    _seed_other_failures(p, _IDS)
    t1, t2 = (_shared_fail_task(p, tmp_path, _pytest_out(_IDS))[0] for _ in range(2))
    now = time.time()
    d._probe_passed(p.db.task(t1), now)
    key = sharedchecks.tip_key(tip, _IDS)
    later = now + 120
    if rc is None:
        later = now + sharedchecks.TIP_TIMEOUT_S + 600
    else:
        d._tip_dir(key).mkdir(parents=True, exist_ok=True)
        (d._tip_dir(key) / "rc").write_text(rc + "\n")
    d._probe_passed(p.db.task(t1), later)
    d._probe_passed(p.db.task(t2), later + 60)
    assert len(tips) == 1
    (fix,) = _fixes(p)
    (inc,) = p.db.q("SELECT * FROM checks_incidents")
    assert inc["fix_task"] == fix["id"] and json.loads(inc["tasks"]) == [t1, t2]
    for tid in (t1, t2):
        res = json.loads(p.db.task(tid)["result"])
        assert res["shared_hold"]["incident"] == inc["id"] and "tip_check" not in res["shared_hold"]
        assert "woke" not in res and not _ready(p, tid)


@pytest.mark.parametrize("rc", ["0", "1"])
def test_a_shared_failure_on_a_head_holding_the_tip_still_reruns_on_the_tip(env, monkeypatch, tmp_path, rc):
    # A head built on the tip can fail by its own change: a size-budget or guard test each change trips
    # with its own code fails the same way (numbers aside) on every such head. Passing on the tip, it is
    # each task's own: no fix task, and the task wakes to fix its change, not to rebase. Failing there,
    # the tip is broken: one incident with one fix task.
    from ttp import sharedchecks
    p = make(env)
    tip = "c" * 40
    d, started, tips = _tip_daemon(p, monkeypatch, tip=tip, holds_tip=True)
    _seed_other_failures(p, _IDS)
    tid = _shared_fail_task(p, tmp_path, _pytest_out(_IDS))[0]
    now = time.time()
    d._probe_passed(p.db.task(tid), now)
    key = sharedchecks.tip_key(tip, _IDS)
    assert tips == [(tip, sorted(_IDS), key)] and not _fixes(p)
    res = json.loads(p.db.task(tid)["result"])
    assert res["shared_hold"]["tip_check"] == key and res["shared_hold"]["holds_tip"] is True
    d._tip_dir(key).mkdir(parents=True, exist_ok=True)
    (d._tip_dir(key) / "rc").write_text(rc + "\n")
    d._probe_passed(p.db.task(tid), now + 120)
    task = p.db.task(tid)
    res = json.loads(task["result"])
    assert len(tips) == 1 and not started
    if rc == "0":
        assert not _fixes(p) and not p.db.q("SELECT id FROM checks_incidents")
        assert "pass on the push-branch tip" in res["woke"] and "your own change" in res["woke"], res
        assert "rebase" not in res["woke"] and res["wake_tier"] == "standard"
        assert task["not_before"] <= now + 120
    else:
        (fix,) = _fixes(p)
        assert res["shared_hold"]["incident"] and "woke" not in res and not _ready(p, tid)


def test_a_missing_check_target_is_never_held_nor_rerun_on_the_tip(env, monkeypatch, tmp_path):
    # The check:<sig> path keeps its single harness repair owner: a check that found no target is no
    # shared test failure, whatever ids its output names.
    p = make(env)
    d, _, tips = _tip_daemon(p, monkeypatch)
    _seed_other_failures(p, _IDS)
    tid, run_dir = _shared_fail_task(p, tmp_path, _pytest_out(_IDS))
    fail = json.loads((run_dir / "check_failure.json").read_text())
    (run_dir / "check_failure.json").write_text(json.dumps({**fail, "kind": "selector_missing", "sig": "abc"}))
    d._probe_passed(p.db.task(tid), time.time())
    res = json.loads(p.db.task(tid)["result"])
    assert res.get("escalated_wake") and "shared_hold" not in res
    assert not tips and not _fixes(p) and not p.db.q("SELECT id FROM checks_incidents")


def test_the_tip_rerun_runs_only_the_failing_ids_in_its_own_worktree_and_removes_it(env, monkeypatch):
    from ttp import daemon as dmod, sharedchecks
    p = make(env)
    git = ["git", "-C", str(p.root), "-c", "user.name=t", "-c", "user.email=t@t"]
    (p.root / "tests_tip").mkdir()
    (p.root / "tests_tip" / "test_tip.py").write_text("def test_ok():\n    pass\n\ndef test_other():\n    assert 0\n")
    subprocess.run([*git, "add", "tests_tip"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "tip"], check=True, capture_output=True)
    tip = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    d = dmod.Daemon(p.base)
    ids = ["tests_tip/test_tip.py::test_ok"]
    key = sharedchecks.tip_key(tip, ids)
    assert d._start_tip_check(tip, ids, key)
    rc = d._tip_dir(key) / "rc"
    deadline = time.time() + 120
    while not rc.exists() and time.time() < deadline:
        time.sleep(0.2)
    out = (d._tip_dir(key) / "out").read_text()
    assert rc.read_text().strip() == "0", out
    assert "1 passed" in out and "test_other" not in out
    # The runner removes its worktree just after writing rc: wait for that, not only rc.
    while (d._tip_dir(key) / "wt").exists() and time.time() < deadline:
        time.sleep(0.2)
    assert not (d._tip_dir(key) / "wt").exists()
    wts = subprocess.run([*git, "worktree", "list"], capture_output=True, text=True).stdout
    assert str(d._tip_dir(key)) not in wts


def test_the_tip_rerun_runs_its_tests_in_the_process_sandbox_like_every_check(env, monkeypatch):
    # Push-branch test code run by the daemon gets its own PID namespace (sandbox.wrap via
    # push.check_argv), so it cannot see or signal the daemon or other workers; still niced.
    from ttp import daemon as dmod, sandbox, sharedchecks, worktree
    p = make(env)
    tip = subprocess.run(["git", "-C", str(p.root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(sandbox, "status", lambda: (["/sb/unshare", "--pid"], ""))
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    real, started = subprocess.Popen, []

    def popen(argv, *a, **kw):
        if argv[:2] == [sys.executable, "-c"] and argv[2] == dmod.TIP_RUNNER:
            started.append((argv, kw))
            return None
        return real(argv, *a, **kw)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setenv(sandbox.ENV, "0")   # restored after the test: the daemon sets it from the config
    from ttp import configwho
    p.set_config("sandbox", {"checks": True}, by=configwho.by("user", via="cli"))   # opt-in, on the user's word
    d = dmod.Daemon(p.base)
    d.cfg = {**d.cfg, "runner": {**(d.cfg.get("runner") or {}), "nice": 7}}
    ids = ["tests/test_a.py::test_x", "tests/test_b.py::test_y[1 2]"]
    key = sharedchecks.tip_key(tip, ids)
    assert d._start_tip_check(tip, ids, key)
    (argv, kw), = started
    child = argv[7:]
    assert child[:3] == ["nice", "-n", "7"]
    assert child[3:5] == ["/sb/unshare", "--pid"]
    assert child[-1].endswith("tests/test_a.py::test_x 'tests/test_b.py::test_y[1 2]'") and "-m pytest -q" in child[-1]
    assert kw["env"]["TTP_NO_DEVICE"] == "1"
    worktree._git(p.root, "worktree", "remove", "--force", str(d._tip_dir(key) / "wt"), check=False)
