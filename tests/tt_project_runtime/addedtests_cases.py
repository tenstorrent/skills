# Added-test rerun tests (addedtests, the ttp_pytest_rerun plugin) of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there): helpers, fixtures and imports
# come from that file, and pytest collects these tests under its name. Add new tests of the rerun of
# the tests a diff adds here, not there.
from __future__ import annotations


def _added_repo(env, tmp_path, monkeypatch, body: str):
    # A repo whose base has tests/test_mod.py with test_old, and a head commit writing `body` to it.
    from ttp import addedtests, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    count = tmp_path / "count"
    head = ("import pathlib\nC = pathlib.Path(%r)\n\n\ndef _bump():\n    n = len(C.read_text()) if C.exists() else 0\n"
            "    C.write_text('x' * (n + 1))\n    return n\n\n\ndef test_old():\n    pass\n" % str(count))
    (repo / "tests").mkdir()
    (repo / "tests" / "test_mod.py").write_text(head)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    base = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "tests" / "test_mod.py").write_text(head + body)
    subprocess.run([*git, "commit", "-qam", "head"], check=True)
    monkeypatch.setattr(cli, "_checks_base", lambda *a: base)
    monkeypatch.setattr(addedtests, "SPREAD", False)   # no wait for a second's fraction between rounds
    runs = lambda: len(count.read_text()) if count.exists() else 0
    return run, runs


_ADDED_CHECK = "python3 -m pytest -q -p no:cacheprovider tests/test_mod.py"


def test_ttp_checks_fails_an_added_test_that_flakes_on_rerun_and_names_it(env, tmp_path, monkeypatch, capsys):
    # It passes the main run (its first call) and every other call after: 2 of the 5 reruns pass.
    from ttp import cli
    run, runs = _added_repo(env, tmp_path, monkeypatch, "\n\ndef test_flaky():\n    assert _bump() % 2 == 0\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", _ADDED_CHECK])
    err = capsys.readouterr().err
    assert e.value.code == 1 and runs() == 6
    assert "added test(s) flaky on rerun: tests/test_mod.py::test_flaky passed 2/5" in err
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["added_tests"]["status"] == "flaky"
    assert rec["added_tests"]["flaky"] == ["tests/test_mod.py::test_flaky"]
    assert json.loads((run / "check_failure.json").read_text())["flaky"] == ["tests/test_mod.py::test_flaky"]


def test_ttp_checks_passes_a_stable_added_test_after_its_reruns_and_reruns_only_it(env, tmp_path, monkeypatch,
                                                                                   capsys):
    from ttp import cli
    run, runs = _added_repo(env, tmp_path, monkeypatch, "\n\ndef test_new():\n    _bump()\n")
    cli.main(["checks", "--", _ADDED_CHECK])
    out = capsys.readouterr().out
    assert runs() == 6, "the main run and 5 reruns"
    assert "reran 1 added test(s) 5 times each: all passed" in out and "check(s) passed" in out
    rec = json.loads((run / "checks.json").read_text())["added_tests"]
    assert rec["status"] == "passed" and rec["tests"] == ["tests/test_mod.py::test_new"]
    assert rec["passes"] == {"tests/test_mod.py::test_new": 5}
    rows = [json.loads(x) for x in (run / "added-tests-rerun.jsonl").read_text().splitlines()]
    assert {r["id"] for r in rows if "id" in r} == {f"tests/test_mod.py::test_new[ttp-rerun-{k}]" for k in range(1, 6)}


def test_ttp_checks_reruns_nothing_without_added_tests(env, tmp_path, monkeypatch, capsys):
    # A head that changes an existing test's body, not its cases, adds none.
    from ttp import cli
    run, runs = _added_repo(env, tmp_path, monkeypatch, "\n\ndef helper():\n    _bump()\n")
    cli.main(["checks", "--", _ADDED_CHECK])
    out = capsys.readouterr().out
    assert runs() == 0 and "rerunning" not in out and "added test" not in out
    assert "added_tests" not in json.loads((run / "checks.json").read_text())
    assert not (run / "added-tests-rerun.jsonl").exists()


def test_ttp_checks_reports_a_capped_rerun_as_skipped_never_passed(env, tmp_path, monkeypatch, capsys):
    from ttp import addedtests, cli
    run, runs = _added_repo(env, tmp_path, monkeypatch, "\n\ndef test_new():\n    _bump()\n")
    monkeypatch.setattr(addedtests, "MAX_TESTS", 0)
    cli.main(["checks", "--", _ADDED_CHECK])
    out = capsys.readouterr().out
    assert runs() == 1, "only the main run"
    assert "rerun of 1 added test(s) SKIPPED, not counted as passed: 1 added tests, over the cap of 0" in out
    assert "the rerun of the added tests was SKIPPED" in out and "all passed" not in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is True and rec["added_tests"]["status"] == "skipped"


def test_ttp_checks_rerun_stopped_at_the_wall_cap_is_a_timeout_not_a_skip(env, tmp_path, monkeypatch, capsys):
    # The main run passes; the rerun of the added test hangs past the run's wall-time cap.
    from ttp import cli
    run, runs = _added_repo(env, tmp_path, monkeypatch,
                            "\n\ndef test_new():\n    import time\n    if _bump():\n        time.sleep(300)\n")
    monkeypatch.setenv("TTP_CHECKS_WALL_CAP_S", "8")
    t0 = time.time()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", _ADDED_CHECK])
    assert e.value.code == 1 and time.time() - t0 < 60
    assert "a timeout, not a pass" in capsys.readouterr().err
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] is False and rec["timed_out"]["check"] == _ADDED_CHECK


def test_added_tests_rerun_past_its_time_cap_is_skipped_unless_a_round_already_failed(tmp_path):
    from ttp import addedtests
    out, tests = tmp_path / "rows.jsonl", [("tests/test_a.py", "test_a")]
    def runner(rows):
        def go(env, timeout):
            assert timeout == addedtests.MAX_SECONDS and env["PYTHONPATH"].startswith(str(addedtests.PLUGIN_DIR))
            assert json.loads(env[addedtests.ENV])["tests"] == [["tests/test_a.py", "test_a"]]
            pathlib.Path(env[addedtests.OUT]).write_text("".join(json.dumps(r) + "\n" for r in rows))
            raise subprocess.TimeoutExpired("pytest", timeout)
        return go
    sel = {"selected": ["tests/test_a.py::test_a"]}
    ok = {"test": "tests/test_a.py::test_a", "round": 1, "id": "x", "failed": False}
    res = addedtests.run(tests, "pytest", runner([sel, ok]), out, {})
    assert res["status"] == "skipped" and "cap of 240 s" in res["why"]
    assert addedtests.lines(res)[0].startswith("rerun of 1 added test(s) SKIPPED, not counted as passed")
    bad = {**ok, "round": 2, "failed": True}
    res = addedtests.run(tests, "pytest", runner([sel, ok, bad]), out, {})
    assert res["status"] == "flaky" and addedtests.summary(res).endswith("test_a passed 1/2")
    # A rerun that exits non-zero with no failing test fails; one that collects none is skipped.
    res = addedtests.run(tests, "pytest", lambda env, t: 3, out, {})
    assert res["status"] == "failed" and "exited 3" in res["why"]
    res = addedtests.run(tests, "pytest", lambda env, t: 5, out, {})
    assert res["status"] == "skipped" and "collects none of them" in res["why"]


def test_added_tests_finds_new_functions_methods_and_cases_against_the_merge_base(tmp_path):
    from ttp import addedtests
    repo = tmp_path / "r"
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tests" / "cases").mkdir(parents=True)
    old = ("import pytest\n\n@pytest.mark.parametrize('x', [1])\ndef test_p(x):\n    pass\n\n"
           "def test_same():\n    pass\n\nclass TestK:\n    def test_m(self):\n        pass\n")
    (repo / "tests" / "test_a.py").write_text(old)
    (repo / "tests" / "move_test.py").write_text("def test_kept():\n    pass\n")
    (repo / "lib.py").write_text("def test_not_a_test_file():\n    pass\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    base = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "tests" / "test_a.py").write_text(
        old.replace("[1]", "[1, 2]").replace("def test_same():\n    pass", "def test_same():\n    return None")
        + "\n\nasync def test_async():\n    pass\n\nclass TestK2:\n    def test_n(self):\n        pass\n"
          "\n\ndef helper_test():\n    pass\n")
    subprocess.run([*git, "mv", "tests/move_test.py", "tests/moved_test.py"], check=True)
    (repo / "tests" / "cases" / "area_cases.py").write_text("def test_case():\n    pass\n")
    (repo / "lib.py").write_text("def test_not_a_test_file():\n    pass\n\ndef test_other():\n    pass\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "head"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    # The base moved on after the branch point: the merge base is what the head is compared with.
    subprocess.run([*git, "checkout", "-q", "-b", "other", base], check=True)
    (repo / "tests" / "test_b.py").write_text("def test_on_base():\n    pass\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "moved"], check=True)
    moved = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert sorted(addedtests.added(repo, moved, head)) == [
        ("tests/cases/area_cases.py", "test_case"), ("tests/test_a.py", "TestK2.test_n"),
        ("tests/test_a.py", "test_async"), ("tests/test_a.py", "test_p")]
    assert addedtests.added(repo, "", head) == [] and addedtests.added(repo, head, head) == []
    assert addedtests.eligible("python3 -m pytest -q tests/test_a.py")
    assert not addedtests.eligible("pytest -q && ruff check .") and not addedtests.eligible("ruff check .")
    assert not addedtests.eligible("pytest -q tests; pytest -q more")


def test_rerun_plugin_spreads_rounds_over_a_second_and_leaves_a_pytest_started_elsewhere_alone(tmp_path):
    # A test that fails in the second half of each second: of 2 rounds, started at fractions 0 and
    # 0.5 of a second, one passes and one fails, however fast the test is.
    from ttp import addedtests
    root, other = tmp_path / "root", tmp_path / "other"
    for d in (root, other):
        d.mkdir()
        (d / "test_t.py").write_text("import time\n\ndef test_half():\n    assert time.time() % 1 < 0.5\n\n"
                                     "def test_kept():\n    pass\n")
    base = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTHONPATH")}
    out = tmp_path / "rows.jsonl"
    env = addedtests.env([("test_t.py", "test_half")], out, {**base, "PWD": str(root)}, 2)
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_t.py"]
    r = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 1, r.stdout
    t = addedtests.tally(out, [("test_t.py", "test_half")], 2)
    assert t == {"selected": ["test_t.py::test_half"], "passes": {"test_t.py::test_half": 1},
                 "ran": {"test_t.py::test_half": 2}, "failed_ids": ["test_t.py::test_half[ttp-rerun-2]"]}
    assert "test_kept" not in r.stdout and "1 failed, 1 passed, 1 deselected" in r.stdout
    # The same environment in another directory (a pytest a test starts): no selection, no rounds.
    out.unlink()
    r = subprocess.run([*argv, "-k", "kept"], cwd=other, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "1 passed, 1 deselected" in r.stdout and not out.exists()
