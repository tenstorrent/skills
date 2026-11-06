# Tests gate judging (gatejudge: failing tests rerun on the head and the base on the same host, the flaky
# quarantine) of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


_FAIL = "python3 -m pytest -p no:cacheprovider tests/"


def _fake_run(head: dict, base: dict, calls: list):
    # run(at, ids): exit 1 with a FAILED line per id the dict marks failing at `at`, else exit 0.
    def run(at, ids):
        calls.append((at, list(ids)))
        bad = [i for i in ids if (head if at == "head" else base).get(i)]
        if not bad:
            return 0, f"{len(ids)} passed in 0.1s\n"
        out = "".join(f"FAILED {i} - assert 0\n" for i in bad)
        return 1, out + f"{len(bad)} failed, {len(ids) - len(bad)} passed in 0.1s\n"
    return run


def test_gate_judge_sorts_pre_existing_flaky_and_new_failures(tmp_path):
    from ttp import gatejudge
    a, b, c = "tests/test_a.py::test_pre", "tests/test_a.py::test_flaky", "tests/test_a.py::test_new"
    calls = []
    run = _fake_run({a: True, b: False, c: True}, {a: True, c: False}, calls)
    v = gatejudge.judge([a, b, c], run, tmp_path, "base1", "host-a", 1000.0)
    assert v["pre_existing"] == [a] and v["flaky"] == [b] and v["new"] == [c]
    assert gatejudge.blocks(v)
    # Only the failing ids rerun: all three on the head, only those failing again on the base.
    assert calls == [("head", [a, b, c]), ("base", [a, c])]
    # Each case alone: pre-existing and flaky never block, a new failure does.
    for head, base, blocking in (({a: True}, {a: True}, False), ({a: False}, {a: True}, False),
                                 ({a: True}, {a: False}, True)):
        v = gatejudge.judge([a], _fake_run(head, base, []), tmp_path / str(len(calls)), "b2", "host-a", 1000.0)
        calls.append(0)
        assert gatejudge.blocks(v) is blocking
    # A rerun that cannot tell (out of time, an incomplete list) keeps the failure.
    v = gatejudge.judge([a], lambda at, ids: (None, ""), tmp_path / "t", "b3", "host-a", 1000.0)
    assert v["new"] == [a]
    v = gatejudge.judge([a], lambda at, ids: (1, "boom\n"), tmp_path / "t", "b3", "host-a", 1000.0)
    assert v["new"] == [a]
    # No base to compare with: a failure again on the head stays new.
    assert gatejudge.judge([a], _fake_run({a: True}, {}, []), tmp_path / "t", "", "host-a", 1000.0)["new"] == [a]
    # A test the base does not have (pytest exit 5, nothing selected) passes there: the failure is new.
    v = gatejudge.judge([a], lambda at, ids: (1, f"FAILED {a} - x\n1 failed in 0.1s\n") if at == "head"
                        else (5, "no tests ran\n"), tmp_path / "t", "b4", "host-a", 1000.0)
    assert v["new"] == [a] and gatejudge.proven(v) == [a]


def test_gate_judge_proves_new_only_from_a_conclusive_head_rerun_and_a_real_base_result(tmp_path):
    """Review #6582: an id the reruns could not tell apart stays `new` (the failure stands) but is never
    `proven`, so it is never saved (save_new) and never ends a spill run; the gate's words say so."""
    from ttp import gatejudge
    a = "tests/test_a.py::test_new"
    head_fail = (1, f"FAILED {a} - x\n1 failed in 0.1s\n")
    cases = {   # the head rerun: out of time, an odd exit, a crash, an incomplete list
        "timeout": lambda at, ids: (None, ""), "exit 2": lambda at, ids: (2, "ERROR: usage\n"),
        "exit 255": lambda at, ids: (255, "ssh: connect\n"), "incomplete": lambda at, ids: (1, "boom\n"),
        # the base rerun cannot tell (out of time, exit 2): no real base result
        "base timeout": lambda at, ids: head_fail if at == "head" else (None, ""),
        "base exit 2": lambda at, ids: head_fail if at == "head" else (2, "ERROR\n"),
    }
    for n, (name, run) in enumerate(cases.items()):
        v = gatejudge.judge([a], run, tmp_path / str(n), "b1", "host-a", 1000.0)
        assert v["new"] == [a] and gatejudge.blocks(v), name
        assert gatejudge.proven(v) == [], name
        gatejudge.save_new(tmp_path, f"tree{n}", "pytest", {**v, "head": "h"}, 1000.0)
        assert gatejudge.known_new(tmp_path, f"tree{n}", "pytest", 1001.0) is None, name
        said = " ".join(gatejudge.lines(v))
        assert "not proven new" in said and "not on the base" not in said, (name, said)
    # No merge base: nothing to compare with, never proven.
    v = gatejudge.judge([a], lambda at, ids: head_fail, tmp_path / "nb", "", "host-a", 1000.0)
    assert v["new"] == [a] and gatejudge.proven(v) == []
    # A saved base result from this host for this base proves it, as a fresh one does.
    gatejudge.save_base(tmp_path / "sb", "host-a", "b1", {a: False}, 1000.0)
    v = gatejudge.judge([a], lambda at, ids: head_fail if at == "head" else (None, ""), tmp_path / "sb", "b1",
                        "host-a", 1000.0)
    assert gatejudge.proven(v) == [a] and "not on the base" in " ".join(gatejudge.lines(v))
    # Only the proven part of a mixed verdict is saved.
    gatejudge.save_new(tmp_path, "mix", "pytest", {"new": [a, "t::u"], "proven": [a], "head": "h"}, 1000.0)
    assert gatejudge.known_new(tmp_path, "mix", "pytest", 1001.0)["new"] == [a]


def test_gate_judge_keeps_a_failure_whose_head_rerun_selected_none_or_not_all_of_it(tmp_path):
    from ttp import gatejudge
    a, b = "tests/test_a.py::test_p[x1]", "tests/test_a.py::test_q"
    # Nothing selected on the head (exit 5, e.g. a parametrize id that changed): not flaky, still new.
    calls = []

    def none_ran(at, ids):
        calls.append(at)
        return 5, "no tests ran in 0.1s\n"
    v = gatejudge.judge([a], none_ran, tmp_path, "b1", "host-a", 1000.0)
    assert v["new"] == [a] and v["flaky"] == [] and calls == ["head"]
    assert gatejudge.active(tmp_path, 1000.0) == {}
    # Exit 0 with fewer passed than rerun (the selection missed one), or no summary: cannot tell.
    for out in ("1 passed in 0.1s\n", "\n"):
        v = gatejudge.judge([a, b], lambda at, ids: (0, out), tmp_path, "b1", "host-a", 1000.0)
        assert v["new"] == [a, b] and v["flaky"] == []
    # Exit 1 listing only the other id, with fewer run than rerun (the selection missed one): the
    # unreported id stays new, even when the other fails on the base too.
    fail_q = (1, f"FAILED {b} - assert 0\n1 failed, 9 deselected in 0.1s\n")
    v = gatejudge.judge([a, b], lambda at, ids: fail_q, tmp_path, "b1", "host-a", 1000.0)
    assert v["new"] == [a, b] and v["flaky"] == [] and v["pre_existing"] == []
    assert gatejudge.active(tmp_path, 1000.0) == {}
    # One passed and one failed (both ran): the passed one is flaky, the other compared on the base.
    v = gatejudge.judge([a, b], lambda at, ids: (1, f"FAILED {b} - assert 0\n1 failed, 1 passed in 0.1s\n"),
                        tmp_path, "b1", "host-a", 1000.0)
    assert v["flaky"] == [a] and v["pre_existing"] == [b] and v["new"] == []
    # Both reported passed: flaky, as before.
    v = gatejudge.judge([a, b], lambda at, ids: (0, "2 passed, 9 deselected in 0.1s\n"), tmp_path, "b1",
                        "host-a", 1000.0)
    assert v["flaky"] == [a, b] and v["new"] == []


def test_gate_rerun_selects_each_failing_id_by_its_full_node_id(tmp_path):
    # A real pytest rerun through the select plugin: a same-named test in another file is never run,
    # so its pass cannot stand in for a failing id that did not run (and get that id quarantined).
    from ttp import gatejudge
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "pytest.ini").write_text("[pytest]\n")
    (proj / "tests" / "test_a.py").write_text("def test_other():\n    pass\n\ndef test_z():\n    assert 0\n")
    (proj / "tests" / "test_b.py").write_text("def test_x():\n    pass\n\ndef test_z():\n    pass\n")
    (proj / "tests" / "test_c.py").write_text("def test_y():\n    pass\n")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "TTP_PYTEST_"))}

    def run(at, ids, cwd=proj):
        e = {**clean, **gatejudge.select_env(ids, clean)}
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--rootdir={proj}"],
                           cwd=str(cwd), env=e, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr
    gone, y = "tests/test_a.py::test_x", "tests/test_c.py::test_y"
    # The failing id no longer runs on the head; only tests/test_b.py::test_x has its name: not flaky.
    assert run("head", [gone])[0] == gatejudge.NO_TESTS
    v = gatejudge.judge([gone], run, tmp_path / "s1", "", "host-a", 1000.0)
    assert v["new"] == [gone] and v["flaky"] == [] and gatejudge.active(tmp_path / "s1", 1000.0) == {}
    # With another failing id that passes, the namesake's pass would make up the count: both stay new.
    v = gatejudge.judge([gone, y], run, tmp_path / "s2", "", "host-a", 1000.0)
    assert v["new"] == [gone, y] and v["flaky"] == []
    # A failing id that runs again fails alone; its passing namesake in test_b.py is deselected.
    z = "tests/test_a.py::test_z"
    rc, out = run("head", [z])
    assert rc == 1 and gatejudge._ran(out) == (0, 1), out
    assert gatejudge.judge([z], run, tmp_path / "s3", "", "host-a", 1000.0)["new"] == [z]
    # Ids as pytest prints them from a subdirectory (relative to the current directory) still select.
    rc, out = run("head", ["test_b.py::test_x"], cwd=proj / "tests")
    assert rc == 0 and gatejudge._ran(out) == (1, 0), out


def test_gate_judge_uses_a_saved_base_result_only_from_the_same_host(tmp_path):
    from ttp import gatejudge
    a = "tests/test_a.py::test_pre"
    gatejudge.save_base(tmp_path, "host-b", "base1", {a: True}, 1000.0)
    calls = []
    v = gatejudge.judge([a], _fake_run({a: True}, {a: False}, calls), tmp_path, "base1", "host-a", 1001.0)
    assert ("base", [a]) in calls, "another host's base result never counts"
    assert v["new"] == [a]
    # Saved on this host by that rerun: the next judge on this host reuses it, no base rerun.
    calls.clear()
    v = gatejudge.judge([a], _fake_run({a: True}, {a: True}, calls), tmp_path, "base1", "host-a", 1002.0)
    assert calls == [("head", [a])] and v["new"] == [a]
    # Another base commit, or the same one past the keep window, is no saved result.
    assert gatejudge.saved_base(tmp_path, "host-a", "base2", [a], 1002.0) == {}
    assert gatejudge.saved_base(tmp_path, "host-a", "base1", [a], 1001.0 + gatejudge.BASE_KEEP_S) == {}
    # A forged key naming this host for a row recorded on another is refused too.
    path = tmp_path / gatejudge.BASE_FILE
    d = json.loads(path.read_text())
    d[f"host-c|base9|{a}"] = {"host": "host-b", "failed": True, "ts": 1000.0}
    path.write_text(json.dumps(d))
    assert gatejudge.saved_base(tmp_path, "host-c", "base9", [a], 1001.0) == {}


def test_gate_quarantine_expires_and_excuses_only_untouched_tests(tmp_path):
    from ttp import gatejudge
    a, b = "tests/test_a.py::test_flaky", "tests/test_b.py::test_other"
    day = 86400.0
    assert gatejudge.quarantine(tmp_path, [a], {"head": "h1"}, 1000.0, 7) == [a]
    assert gatejudge.quarantine(tmp_path, [a, b], {"head": "h2"}, 1000.0 + day, 7) == [b], "one note per test"
    e = gatejudge.active(tmp_path, 1000.0 + day)[a]
    assert e["first_seen"] == 1000.0 and e["expires"] == 1000.0 + 8 * day and len(e["evidence"]) == 2
    assert set(gatejudge.active(tmp_path, 1000.0 + 7.9 * day)) == {a, b}
    assert gatejudge.active(tmp_path, 1000.0 + 8 * day + 1) == {}, "expired after its days"
    assert any(a in line for line in gatejudge.status_lines(tmp_path, 1000.0 + day))
    assert gatejudge.status_lines(tmp_path, 1000.0 + 9 * day) == []
    # Expired entries go on the next write; a test seen flaky again starts a new record.
    assert gatejudge.quarantine(tmp_path, [a], {}, 1000.0 + 9 * day, 7) == [a]
    assert set(json.loads((tmp_path / gatejudge.QUARANTINE_FILE).read_text())) == {a}
    # A quarantined test the change does not touch is excused without a rerun; one it touches reruns.
    calls = []
    run = _fake_run({a: True}, {a: False}, calls)
    v = gatejudge.judge([a], run, tmp_path, "base1", "host-a", 1000.0 + 9 * day, touched=lambda i: False)
    assert v["quarantined"] == [a] and not gatejudge.blocks(v) and calls == []
    v = gatejudge.judge([a], run, tmp_path, "base1", "host-a", 1000.0 + 9 * day, touched=lambda i: True)
    assert v["new"] == [a] and calls
    # Past expiry it is judged again in full.
    calls.clear()
    v = gatejudge.judge([a], run, tmp_path, "base1", "host-a", 1000.0 + 17 * day, touched=lambda i: False)
    assert v["new"] == [a] and calls
    # checks.quarantine_days sets the expiry; a bad value keeps the default.
    assert gatejudge.settings({"checks": {"quarantine_days": 2}})["days"] == 2.0
    assert gatejudge.settings({"checks": {"quarantine_days": "x"}})["days"] == gatejudge.DAYS
    assert gatejudge.settings({"checks": {"quarantine_days": True}})["days"] == gatejudge.DAYS


def test_gate_excused_pass_never_counts_for_a_release_or_whole_suite_check():
    from ttp import gatejudge
    assert gatejudge.counts_as_passed({"passed": True}, release=True)
    assert gatejudge.counts_as_passed({"passed": True, "excused": [{"check": "x"}]}, release=False)
    assert not gatejudge.counts_as_passed({"passed": True, "excused": [{"check": "x"}]}, release=True)
    assert not gatejudge.counts_as_passed({"passed": False}, release=False)


def _gate_repo(env, tmp_path, monkeypatch, base_body: str, head_body: str):
    # A repo whose base has tests/test_gate.py with `base_body` and whose head writes `head_body` there.
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_gate.py").write_text(base_body)
    (repo / "b.txt").write_text("base\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    base = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "tests" / "test_gate.py").write_text(head_body)
    (repo / "b.txt").write_text("head\n")
    subprocess.run([*git, "commit", "-qam", "head"], check=True)
    monkeypatch.setattr(cli, "_checks_base", lambda *a: base)
    return cli.Project(os.environ["TTP_PROJECT"]), run


_PRE = "def test_ok():\n    pass\n\n\ndef test_pre():\n    assert False\n"


def test_ttp_checks_lets_a_failure_on_base_and_head_land_but_records_no_pass(env, tmp_path, monkeypatch, capsys):
    # test_pre fails on the base and the head (same host): pre-existing. The change's landing goes on;
    # the pass is landing-only (not recorded, so a later run runs it again), and --release fails on it.
    from ttp import cli, gatejudge
    p, run = _gate_repo(env, tmp_path, monkeypatch, _PRE, _PRE + "\n\ndef helper():\n    return 1\n")
    cli.main(["checks", "--", _FAIL])
    out = capsys.readouterr().out
    assert "pre-existing on the base" in out and "tests/test_gate.py::test_pre" in out and "LANDING ONLY" in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] and rec["landing_only"]
    assert rec["excused"][0]["pre_existing"] == ["tests/test_gate.py::test_pre"]
    assert rec["excused"][0]["host"] == gatejudge.host()
    assert not gatejudge.counts_as_passed(rec, release=True)
    path = p.state / cli.CHECK_PASSES
    assert not path.exists() or not json.loads(path.read_text())["passes"], "an excused pass is never recorded"
    assert not gatejudge.active(p.state), "nothing flaky: nothing quarantined"
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--release", "--", _FAIL])
    assert e.value.code == 1
    assert not json.loads((run / "checks.json").read_text())["passed"]


def test_ttp_checks_blocks_a_failure_new_on_the_head(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    base = "def test_ok():\n    pass\n\n\ndef test_x():\n    pass\n"
    p, run = _gate_repo(env, tmp_path, monkeypatch, base, base.replace("def test_x():\n    pass", "def test_x():\n    assert False"))
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", _FAIL])
    assert e.value.code == 1
    assert "new on this head" in capsys.readouterr().out
    assert not json.loads((run / "checks.json").read_text())["passed"]


def test_ttp_checks_a_local_failure_whose_base_rerun_cannot_tell_fails_and_saves_nothing(env, tmp_path, monkeypatch,
                                                                                         capsys):
    """Review #6582: the head rerun fails the test again but the base rerun cannot tell (here out of time): the
    failure stands, unproven, and gate_new.json keeps nothing, so the tree's next run is not failed by it."""
    from ttp import cli, gatejudge
    base = "def test_ok():\n    pass\n\n\ndef test_x():\n    pass\n"
    p, run = _gate_repo(env, tmp_path, monkeypatch, base, base.replace("def test_x():\n    pass", "def test_x():\n    assert False"))
    real = gatejudge.rerun
    monkeypatch.setattr(gatejudge, "rerun", lambda run, at, ids, *a, **k: real(run, at, ids, *a, **k) if at == "head"
                        else (None, ""))
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", _FAIL])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "not proven new" in out and "new on this head" not in out, out
    assert not json.loads((run / "checks.json").read_text())["passed"]
    assert not (p.state / gatejudge.NEW_FILE).exists() or not json.loads((p.state / gatejudge.NEW_FILE).read_text())


def test_ttp_checks_quarantines_a_flaky_failure_and_notes_the_coordinator_once(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, gatejudge
    count = tmp_path / "count"
    flaky = (f"import pathlib\nC = pathlib.Path({str(count)!r})\n\n\ndef test_flaky():\n"
             "    n = len(C.read_text()) if C.exists() else 0\n    C.write_text('x' * (n + 1))\n    assert n % 2\n")
    p, run = _gate_repo(env, tmp_path, monkeypatch, flaky, flaky + "\n\ndef helper():\n    return 1\n")
    cli.main(["checks", "--", _FAIL])
    out = capsys.readouterr().out
    assert "flaky" in out and "quarantined" in out
    q = gatejudge.active(p.state)
    assert list(q) == ["tests/test_gate.py::test_flaky"] and q[next(iter(q))]["evidence"][0]["head"]
    notes = p.db.q("SELECT text FROM events WHERE kind=? AND status='queued'", (gatejudge.EVENT,))
    assert len(notes) == 1 and "test_flaky" in notes[0]["text"]
    assert "quarantined test tests/test_gate.py::test_flaky" in cli.status_text(p)


def test_a_detached_release_check_keeps_release_across_a_daemon_restart(env, monkeypatch):
    # checks.pid records --release and the daemon's restart (reboot, shared failure, rebase) passes it
    # on: a restarted release run that lost it would excuse failures and exit 0.
    import argparse
    from ttp import cli, worktree
    from ttp import daemon as dmod
    p = make(env)
    run_dir = env["tmp"] / "rel"
    run_dir.mkdir()
    argvs = []
    real = subprocess.Popen

    def popen(argv, **kw):   # the launcher's child: recorded, then a stand-in that waits for its go
        argvs.append(list(argv))
        return real([sys.executable, "-c", "import sys; sys.stdin.read()"], **kw)
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    a = argparse.Namespace(fresh=False, full=False, light=False, no_device=False, release=True, purpose=None,
                           cmd=["true"])
    pid = cli._checks_start(a, run_dir, "", "a" * 40, {**os.environ, "TTP_PROJECT": str(p.base)})
    os.waitpid(pid, 0)
    info = cli._read_checks_pid(run_dir)
    assert "--release" in argvs[0] and info["release"] is True
    monkeypatch.setattr(worktree, "is_git", lambda _p: True)
    monkeypatch.setattr(worktree, "_git", lambda _p, *args: "a" * 40 if args[0] == "rev-parse" else "")
    d = dmod.Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "_start_checks", lambda path, out, run_id, tid, extra: started.append(extra) or True)
    assert d._restart_checks(1, run_dir, {**info, "worktree": str(env["tmp"])}, "r1") == ""
    assert "--release" in started[0] and started[0][-1] == "true"
    assert d._restart_checks(1, run_dir, {**info, "worktree": str(env["tmp"]), "release": False}, "r1") == ""
    assert "--release" not in started[1]


def test_checks_result_passed_fails_a_release_run_that_excused_a_failure(env, tmp_path, capsys):
    from ttp import cli
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "checks.rc").write_text("0\n")

    def passed(pid_release, rec):
        (run_dir / "checks.pid").write_text(json.dumps({"pid": os.getpid(), "start": 0, "ts": 10.0,
                                                        "release": pid_release}))
        (run_dir / "checks.json").unlink(missing_ok=True)
        if rec is not None:
            (run_dir / "checks.json").write_text(json.dumps({"passed": True, "ts": 20.0, **rec}))
        return cli._checks_passed(run_dir)
    excused = {"excused": [{"check": _FAIL, "pre_existing": ["tests/t.py::test_a"]}], "landing_only": True}
    assert passed(True, excused) == 1 and "excused" in capsys.readouterr().out
    assert passed(False, {**excused, "release": True}) == 1, "checks.json's release mark alone also holds"
    assert passed(True, None) == 1, "a release run with no record of its own run"
    assert passed(True, {**excused, "ts": 5.0}) == 1, "an earlier run's record is no record of this one"
    assert passed(True, {}) == 0
    assert passed(False, excused) == 0, "a landing check's excused pass still counts for its landing"


def _busy_run(results: dict, calls: list):
    # run(at, ids): pops the next (rc, output) queued for `at` in `results`.
    def run(at, ids):
        calls.append((at, list(ids)))
        return results[at].pop(0)
    return run


def test_gate_judge_retries_a_busy_base_rerun_on_the_same_host_then_judges_it(tmp_path):
    from ttp import gatejudge
    i = "tests/test_a.py::test_a"
    fail = (1, f"FAILED {i} - assert 0\n1 failed in 0.1s\n")
    calls, slept = [], []
    run = _busy_run({"head": [fail], "base": [(75, "ADMISSION TIMEOUT\n"), fail]}, calls)
    v = gatejudge.judge([i], run, tmp_path, "b" * 40, "h1", 1000.0, sleep=slept.append)
    assert v["pre_existing"] == [i] and not v["new"] and not v.get("busy")
    assert [c[0] for c in calls] == ["head", "base", "base"] and slept == [gatejudge.BUSY_FIRST_S]
    assert not gatejudge.blocks(v) and not gatejudge.undecided(v)


def test_gate_judge_a_base_rerun_still_busy_is_undecided_never_new(tmp_path):
    from ttp import gatejudge
    i = "tests/test_a.py::test_a"
    fail = (1, f"FAILED {i} - assert 0\n1 failed in 0.1s\n")
    calls, slept = [], []
    run = _busy_run({"head": [fail], "base": [(75, "")] * 9}, calls)
    v = gatejudge.judge([i], run, tmp_path, "b" * 40, "h1", 1000.0, tries=2, wait_s=600, sleep=slept.append)
    assert v["busy"] == [i] and not v["new"] and not v["pre_existing"]
    assert not gatejudge.blocks(v) and gatejudge.undecided(v)
    assert len(calls) == 4 and slept == [30.0, 60.0], "tries caps the retries; the backoff doubles"
    assert any("no free slot" in line for line in gatejudge.lines(v))


def test_gate_judge_a_busy_head_rerun_is_undecided_and_the_wait_is_capped(tmp_path):
    from ttp import gatejudge
    i = "tests/test_a.py::test_a"
    calls, slept = [], []
    run = _busy_run({"head": [(75, "")] * 9, "base": []}, calls)
    v = gatejudge.judge([i], run, tmp_path, "b" * 40, "h1", 1000.0, tries=5, wait_s=50, sleep=slept.append)
    assert v["busy"] == [i] and not v["new"] and gatejudge.undecided(v)
    assert [c[0] for c in calls] == ["head"] * 3 and slept == [30.0, 20.0], "wait_s caps the total backoff"


def test_gate_judge_busy_settings_come_from_checks_config():
    from ttp import gatejudge
    s = gatejudge.settings({"checks": {"judge_busy_tries": 1, "judge_busy_wait_s": 5}})
    assert s["busy_tries"] == 1 and s["busy_wait_s"] == 5
    d = gatejudge.settings({})
    assert d["busy_tries"] == gatejudge.BUSY_TRIES and d["busy_wait_s"] == gatejudge.BUSY_WAIT_S


def test_gate_judge_busy_reruns_and_their_backoff_are_pool_wait_of_the_push_check_it_runs_in(monkeypatch, tmp_path):
    """Inside a push check, a head or base rerun that exits BUSY ran nothing: its try and the backoff after it
    are recorded as a test-pool wait (checkwait), so the check's time limit never counts them; the try that
    runs is run time."""
    from ttp import checkwait, gatejudge
    waits = tmp_path / "waits"
    waits.mkdir()
    monkeypatch.setenv(checkwait.ENV, str(waits))
    clock = [1000.0]
    monkeypatch.setattr(gatejudge.time, "time", lambda: clock[0])
    i = "tests/test_a.py::test_a"
    fail = (1, f"FAILED {i} - assert 0\n1 failed in 0.1s\n")
    results = {"head": [(75, ""), (75, ""), fail], "base": [(0, "1 passed in 0.1s\n")]}

    def run(at, ids):
        clock[0] += 240.0 if results[at][0][0] == 75 else 100.0   # a busy try waits 240 s for admission
        return results[at].pop(0)

    def sleep(s):
        clock[0] += s
    v = gatejudge.judge([i], run, tmp_path / "state", "b" * 40, "h1", 1000.0, sleep=sleep)
    assert v["new"] == [i]
    pooled, live = checkwait.pool(waits, 1000.0, clock[0])
    assert not live and pooled == 240 + 30 + 240 + 60, pooled   # the two runs that ran (200 s) are not wait
    assert checkwait.state(waits, 1000.0, clock[0]) == (pooled, False)
    monkeypatch.delenv(checkwait.ENV)
    results["head"] = [(75, ""), fail]
    gatejudge.rerun(run, "head", [i], sleep=sleep)   # outside a push check: records nothing, still retries
    assert not results["head"]


def _busy_base_repo(env, tmp_path, monkeypatch, busy_runs: int):
    # test_pre fails on base and head; the base checkout's pytest exits 75 (no free slot) for its first
    # `busy_runs` runs, as a test pool's admission timeout does, then runs.
    count = tmp_path / "base-runs"
    body = (f"import os\nimport pathlib\n\nif (pathlib.Path(__file__).parent.parent / 'b.txt').read_text() == 'base\\n':\n"
            f"    C = pathlib.Path({str(count)!r})\n    n = len(C.read_text()) if C.exists() else 0\n"
            f"    C.write_text('x' * (n + 1))\n    if n < {busy_runs}:\n"
            f"        print('admission timeout: no free slot', flush=True)\n        os._exit(75)\n\n\n" + _PRE)
    p, run = _gate_repo(env, tmp_path, monkeypatch, body, body + "\n\ndef helper():\n    return 1\n")
    p.set_config("checks.judge_busy_wait_s", 0.01)
    return p, run, count


def test_ttp_checks_retries_a_busy_base_rerun_then_lets_a_failure_on_base_and_head_land(env, tmp_path, monkeypatch, capsys):
    # The base rerun exits 75 once, is retried on the same host and then judged: pre-existing, not new and
    # not undecided, so the change lands (landing only).
    from ttp import cli
    p, run, count = _busy_base_repo(env, tmp_path, monkeypatch, 1)
    cli.main(["checks", "--", _FAIL])
    out = capsys.readouterr().out
    assert len(count.read_text()) == 2, "the busy base rerun ran again"
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] and rec["landing_only"] and "infra_fault" not in rec, out
    assert rec["excused"][0]["pre_existing"] == ["tests/test_gate.py::test_pre"] and not rec["excused"][0]["busy"]


def test_ttp_checks_a_base_rerun_still_busy_after_its_retries_exits_75_never_a_head_failure(
        env, tmp_path, monkeypatch, capsys):
    # Busy on every try (checks.judge_busy_tries bounds them): no verdict, so the run ends exit 75 with
    # infra_fault gate-rerun-busy, never 1 and never a pass.
    from ttp import cli, gatejudge
    p, run, count = _busy_base_repo(env, tmp_path, monkeypatch, 99)
    monkeypatch.setattr(gatejudge, "BUSY_FIRST_S", 0.01)
    p.set_config("checks.judge_busy_wait_s", 5)
    p.set_config("checks.judge_busy_tries", 2)
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", _FAIL])
    out = capsys.readouterr()
    assert e.value.code == 75, out
    assert len(count.read_text()) == 3, "one run and its 2 retries"
    rec = json.loads((run / "checks.json").read_text())
    assert not rec["passed"] and rec["infra_fault"] == "gate-rerun-busy", rec
    assert "new on this head" not in out.out + out.err


def test_a_blocking_verdict_is_saved_per_tree_and_check_and_expires(tmp_path):
    """#6509: save_new keeps a blocking verdict per head tree and check (only one with new ids); known_new
    finds it for that tree and check alone until BASE_KEEP_S passes, and its text says to fix the tests."""
    from ttp import gatejudge
    v = {"new": ["t.py::test_x"], "proven": ["t.py::test_x"], "head": "a" * 40, "base": "b" * 40, "host": "box"}
    gatejudge.save_new(tmp_path, "tree1", "pytest", {**v, "new": [], "proven": []}, 100.0)
    assert gatejudge.known_new(tmp_path, "tree1", "pytest", 101.0) is None, "a verdict that blocks nothing"
    gatejudge.save_new(tmp_path, "tree1", "pytest", v, 100.0)
    e = gatejudge.known_new(tmp_path, "tree1", "pytest", 101.0)
    assert e and e["new"] == ["t.py::test_x"] and e["host"] == "box", e
    assert gatejudge.known_new(tmp_path, "tree2", "pytest", 101.0) is None
    assert gatejudge.known_new(tmp_path, "tree1", "pytest -x", 101.0) is None
    assert gatejudge.known_new(tmp_path, "tree1", "pytest", 100.0 + gatejudge.BASE_KEEP_S + 1) is None
    said = gatejudge.known_new_text(e, "pytest")
    assert "t.py::test_x" in said and "aaaaaaaaaaaa" in said and "Fix them" in said and "--fresh" in said, said
