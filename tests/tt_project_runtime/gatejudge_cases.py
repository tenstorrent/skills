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
    # Only the failing ids rerun: all three on the base, only those not failing there on the head.
    assert calls == [("base", [a, b, c]), ("head", [b, c])]
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


def test_gate_judge_proven_holds_only_ids_the_head_rerun_failed_again_and_the_base_passed(tmp_path):
    """`new` keeps a failure a rerun could not tell about; `proven` names only one a parsed head rerun failed
    again and a parsed (or saved) base rerun passed, so a spill host's fault is never read as proof."""
    from ttp import gatejudge
    a, c = "tests/test_a.py::test_pre", "tests/test_a.py::test_new"
    v = gatejudge.judge([a, c], _fake_run({a: True, c: True}, {a: True, c: False}, []), tmp_path / "1", "b1",
                        "host-a", 1000.0)
    assert v["new"] == [c] and v["proven"] == [c]
    # The saved base result of that run proves it again without a base rerun.
    v = gatejudge.judge([c], _fake_run({c: True}, {}, []), tmp_path / "1", "b1", "host-a", 1000.0)
    assert v["new"] == [c] and v["proven"] == [c]
    hit = f"FAILED {c} - x\n1 failed in 0.1s\n"
    cases = (lambda at, ids: (255, "ssh: connection reset\n"),          # head rerun: ssh fault
             lambda at, ids: (None, ""),                                    # head rerun: timed out
             lambda at, ids: (1, "boom\n"),                                # head rerun: incomplete list
             lambda at, ids: (1, hit) if at == "head" else (255, ""),      # base rerun could not run
             lambda at, ids: (1, hit) if at == "head" else (None, ""))     # base rerun timed out
    for n, run in enumerate(cases):
        v = gatejudge.judge([c], run, tmp_path / f"u{n}", "b2", "host-a", 1000.0)
        assert v["new"] == [c] and v["proven"] == [] and gatejudge.blocks(v), (n, v)
    # No base to compare with: blocks, never proven.
    v = gatejudge.judge([c], _fake_run({c: True}, {}, []), tmp_path / "2", "", "host-a", 1000.0)
    assert v["new"] == [c] and v["proven"] == []


def test_gate_judge_never_reruns_on_the_head_an_id_that_fails_on_the_base(tmp_path):
    # Many failing files that fail on the base too, one failure that does not: only that one reruns on the
    # head; the rest are failing on both and do not block, whether the base was rerun or saved earlier.
    from ttp import gatejudge
    pre = [f"tests/test_f{n}.py::test_x" for n in range(14)]
    new, flaky = "tests/test_g.py::test_new", "tests/test_g.py::test_flaky"
    calls = []
    run = _fake_run({**{i: True for i in pre}, new: True}, {i: True for i in pre}, calls)
    v = gatejudge.judge([*pre, new, flaky], run, tmp_path, "base1", "host-a", 1000.0)
    assert calls == [("base", [*pre, new, flaky]), ("head", [new, flaky])]
    assert v["pre_existing"] == pre and v["new"] == [new] and v["flaky"] == [flaky]
    assert any(line.startswith("failing on both") and "not rerun on the head" in line for line in gatejudge.lines(v))
    # Saved on this host for this base: neither rerun runs for them.
    calls.clear()
    v = gatejudge.judge(pre, run, tmp_path, "base1", "host-a", 1001.0)
    assert calls == [] and v["pre_existing"] == pre and not gatejudge.blocks(v)
    # No slot for the base: the head rerun still runs; failing again there is undecided, never excused or new.
    i = pre[0]
    v = gatejudge.judge([i, flaky], _busy_run({"base": [(75, "")] * 9, "head": [
        (1, f"FAILED {i} - assert 0\n1 failed, 1 passed in 0.1s\n")]}, calls), tmp_path / "busy", "b2", "host-a",
        1000.0, tries=1, wait_s=1, sleep=lambda s: None)
    assert v["busy"] == [i] and v["flaky"] == [flaky] and not v["new"] and not v["pre_existing"]
    assert gatejudge.undecided(v)


def test_gate_judge_keeps_a_failure_whose_head_rerun_selected_none_or_not_all_of_it(tmp_path):
    from ttp import gatejudge
    a, b = "tests/test_a.py::test_p[x1]", "tests/test_a.py::test_q"
    # Nothing selected on the head (exit 5, e.g. a parametrize id that changed): not flaky, still new.
    calls = []

    def none_ran(at, ids):
        calls.append(at)
        return 5, "no tests ran in 0.1s\n"
    v = gatejudge.judge([a], none_ran, tmp_path, "b1", "host-a", 1000.0)
    assert v["new"] == [a] and v["flaky"] == [] and calls == ["base", "head"]
    assert gatejudge.active(tmp_path, 1000.0) == {}
    # Exit 0 with fewer passed than rerun (the selection missed one), or no summary: cannot tell.
    for out in ("1 passed in 0.1s\n", "\n"):
        v = gatejudge.judge([a, b], lambda at, ids: (0, out), tmp_path, "b1", "host-a", 1000.0)
        assert v["new"] == [a, b] and v["flaky"] == []
    # Exit 1 listing only the other id, with fewer run than rerun (the selection missed one): the
    # unreported id stays new. A base rerun that missed it excuses nothing either: it reruns on the head.
    fail_q = (1, f"FAILED {b} - assert 0\n1 failed, 9 deselected in 0.1s\n")
    v = gatejudge.judge([a, b], lambda at, ids: fail_q if at == "head" else (0, "2 passed in 0.1s\n"),
                        tmp_path / "q1", "b1", "host-a", 1000.0)
    assert v["new"] == [a, b] and v["flaky"] == [] and v["pre_existing"] == []
    seen = []
    v = gatejudge.judge([a, b], lambda at, ids: (seen.append((at, ids)), fail_q)[1], tmp_path / "q2", "b1",
                        "host-a", 1000.0)
    assert v["pre_existing"] == [b] and seen == [("base", [a, b]), ("head", [a])]
    assert gatejudge.active(tmp_path, 1000.0) == {}
    # One passed and one failed (both ran): the passed one is flaky, the other compared on the base.
    v = gatejudge.judge([a, b], lambda at, ids: (1, f"FAILED {b} - assert 0\n1 failed, 1 passed in 0.1s\n"),
                        tmp_path / "q3", "b1", "host-a", 1000.0)
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


def test_the_select_plugin_matches_a_cwd_relative_id_without_config_cwd_relative_nodeid(tmp_path, monkeypatch):
    # pytest 9's config.cwd_relative_nodeid takes a node id object, not the item's string: the plugin
    # computes the cwd-relative id itself, so a gate rerun by id works under either pytest.
    import importlib
    import types
    sel = importlib.import_module("ttp.pytest_resume.ttp_pytest_select")

    def no_str(nodeid):
        raise AssertionError("cwd_relative_nodeid takes no str")
    root = tmp_path / "proj"
    config = types.SimpleNamespace(
        rootpath=root, invocation_params=types.SimpleNamespace(dir=root / "tests"),
        cwd_relative_nodeid=no_str, hook=types.SimpleNamespace(pytest_deselected=lambda items: None))
    items = [types.SimpleNamespace(nodeid=n) for n in ("tests/test_b.py::test_x", "tests/test_a.py::test_x")]
    monkeypatch.delenv("TTP_PYTEST_SELECT", raising=False)
    monkeypatch.delenv("TTP_PYTEST_SELECT_FILES", raising=False)
    monkeypatch.setenv("TTP_PYTEST_SELECT_IDS", "test_b.py::test_x\n")
    for k in ("PYTEST_ADDOPTS", "PYTHONPATH"):   # the configure strip edits them: restored after
        monkeypatch.setenv(k, os.environ.get(k, ""))
    sel.pytest_configure(config)
    sel.pytest_collection_modifyitems(config, items)
    assert [i.nodeid for i in items] == ["tests/test_b.py::test_x"]
    # Run from the root dir, the id is unchanged.
    config.invocation_params.dir = root
    assert sel._cwd_relative(config, "tests/test_b.py::test_x") == "tests/test_b.py::test_x"

def test_gate_rerun_selection_stays_in_its_own_pytest_run(tmp_path):
    # A rerun failing test that starts its own pytest (with the inherited environment) once had all of
    # that inner run's tests deselected (exit 5): the selection acts only in the run it was given to,
    # its xdist workers included (they report no deselected count), and the inner run gets no variables.
    import importlib.util
    from ttp import gatejudge
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "pytest.ini").write_text("[pytest]\n")
    (proj / "inner").mkdir()
    (proj / "inner" / "test_inner.py").write_text(
        "import pytest\n@pytest.mark.parametrize('i', range(3))\ndef test_i(i):\n    pass\n")
    (proj / "tests" / "test_outer.py").write_text(
        "import os, subprocess, sys\n"
        "def test_starts_pytest():\n"
        "    assert not [k for k in os.environ if k.startswith('TTP_PYTEST_SELECT')]\n"
        "    r = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'inner'],\n"
        "                       capture_output=True, text=True, timeout=120)\n"
        "    assert r.returncode == 0 and '3 passed' in r.stdout, r.stdout + r.stderr\n"
        "def test_other():\n    pass\n")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "TTP_PYTEST_"))}
    env = {**clean, **gatejudge.select_env(["tests/test_outer.py::test_starts_pytest"], clean)}
    xdist = [["-n", "2"]] if importlib.util.find_spec("xdist") else []
    for extra in [[], ["-p", "no:xdist"], *xdist]:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--rootdir={proj}",
                            *extra, "tests"], cwd=str(proj), env=env, capture_output=True, text=True, timeout=180)
        out = r.stdout + r.stderr
        assert r.returncode == 0 and gatejudge._ran(out) == (1, 0), (extra, out)
        assert "-n" in extra or "1 deselected" in out, (extra, out)


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
    assert "(rerun on the base" in out and "(rerun on the head" not in out, "failing on the base: no head rerun"
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
    from ttp import checkdirs, cli, worktree
    from ttp import daemon as dmod
    p = make(env)
    run_dir = env["tmp"] / "rel"
    run_dir.mkdir()
    argvs, children = [], []
    real = subprocess.Popen

    def popen(argv, **kw):   # the launcher's child: recorded, then a stand-in that waits for its go
        if list(argv[1:4]) != ["-m", "ttp", "checks"]:   # other calls (process reads on macOS): real
            return real(argv, **kw)
        argvs.append(list(argv))
        children.append(real([sys.executable, "-c", "import sys; sys.stdin.read()"], **kw))
        return children[-1]
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    # The launcher returns only once its child ended (as on a loaded host), so its dropped Popen reaps
    # the child: the test waits through its own handle, never os.waitpid(pid), which then has none.
    cleanup = checkdirs.cleanup
    monkeypatch.setattr(checkdirs, "cleanup", lambda *x, **k: (children[0].wait(timeout=60), cleanup(*x, **k)))
    a = argparse.Namespace(fresh=False, full=False, light=False, no_device=False, release=True, purpose=None,
                           cmd=["true"])
    pid = cli._checks_start(a, run_dir, "", "a" * 40, {**os.environ, "TTP_PROJECT": str(p.base)})
    assert children[0].pid == pid and children[0].wait(timeout=60) == 0
    info = cli._read_checks_pid(run_dir)
    assert "--release" in argvs[0] and info["release"] is True
    monkeypatch.setattr(worktree, "is_git", lambda _p: True)
    monkeypatch.setattr(worktree, "_git", lambda _p, *args: "a" * 40 if args[0] == "rev-parse" else "")
    d = dmod.Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "_start_checks", lambda path, out, run_id, tid, extra, arrived=None: started.append(extra) or True)
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
    assert [c[0] for c in calls] == ["base", "base"] and slept == [gatejudge.BUSY_FIRST_S]
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
    run = _busy_run({"head": [(75, "")] * 9, "base": [(0, "1 passed in 0.1s\n")]}, calls)
    v = gatejudge.judge([i], run, tmp_path, "b" * 40, "h1", 1000.0, tries=5, wait_s=50, sleep=slept.append)
    assert v["busy"] == [i] and not v["new"] and gatejudge.undecided(v)
    assert [c[0] for c in calls] == ["base"] + ["head"] * 3 and slept == [30.0, 20.0], \
        "wait_s caps the total backoff"


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


def _stacked_repo(env, tmp_path, monkeypatch):
    # The gate's base for a task head: a temp repo whose `main` is the push branch tip (test_x passes),
    # a scratch HOME, and a code task (TTP_TASK). Returns (p, run, git, commit, task id, push tip).
    from ttp import cli
    for var, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "xdg"), ("GH_CONFIG_DIR", "gh")):
        (tmp_path / "scratch" / sub).mkdir(parents=True)
        monkeypatch.setenv(var, str(tmp_path / "scratch" / sub))
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    (repo / "tests").mkdir()

    def commit(body: str | None, msg: str) -> str:
        if body is not None:
            (repo / "tests" / "test_gate.py").write_text(body)
        (repo / "n.txt").write_text(msg + "\n")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", msg], check=True)
        return subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    tip = commit(_GATE_OK, "push tip")
    subprocess.run([*git, "branch", "-M", "main"], check=True)
    monkeypatch.setattr(cli, "_checks_base", lambda *a: tip)
    tid = p.db.add_task("A change", "s", kind="code")
    monkeypatch.setenv("TTP_TASK", str(tid))
    return p, run, git, commit, tid, tip


_GATE_OK = "def test_ok():\n    pass\n\n\ndef test_x():\n    pass\n"
_GATE_BAD = _GATE_OK.replace("def test_x():\n    pass", "def test_x():\n    assert False")


@pytest.mark.parametrize("how", ["fork_point", "pr_branch"])
def test_ttp_checks_judges_a_head_stacked_on_a_local_branch_against_what_it_was_built_on(
        env, tmp_path, monkeypatch, capsys, how):
    # A head built on a local merge branch 3 commits past the push branch, where test_x already fails:
    # the gate reruns on the commit the head was built on (its recorded fork point, else its merge base
    # with the branch it is stacked on), so test_x is not new on the head; the push tip would call it new.
    from ttp import cli, worktree
    p, run, git, commit, tid, tip = _stacked_repo(env, tmp_path, monkeypatch)
    subprocess.run([*git, "checkout", "-qb", "merge/demo-batch"], check=True)
    commit(None, "batch 1")
    commit(_GATE_BAD, "batch 2")
    built_on = commit(None, "batch 3")
    subprocess.run([*git, "checkout", "-qb", "ttp/t1-demo"], check=True)
    commit(_GATE_BAD + "\n\ndef helper():\n    return 1\n", "the change")
    if how == "fork_point":
        worktree.record_fork_point(Path.cwd(), tid, built_on)
        why = "fork point recorded"
    else:
        p.db.update_task(tid, labels=json.dumps(["pr_branch:merge/demo-batch"]))
        subprocess.run([*git, "checkout", "-q", "merge/demo-batch"], check=True)
        commit(None, "batch 4")   # the batch moves on after the fork: its merge base with the head stays
        subprocess.run([*git, "checkout", "-q", "ttp/t1-demo"], check=True)
        why = "merge base with heads/merge/demo-batch, the branch it is stacked on"
    cli.main(["checks", "--", _FAIL])
    out = capsys.readouterr().out
    assert f"base {built_on[:12]}: " in out and why in out, out
    assert "pre-existing on the base" in out and "tests/test_gate.py::test_x" in out and "LANDING ONLY" in out
    rec = json.loads((run / "checks.json").read_text())
    assert rec["passed"] and rec["excused"][0]["base"] == built_on and why in rec["excused"][0]["base_why"]
    assert tip != built_on


def test_ttp_checks_judges_a_plain_head_against_the_push_branch_as_before(env, tmp_path, monkeypatch, capsys):
    # A head forked from the push branch: a failure it adds is new (base: its fork point, the push tip).
    # Once the push branch moves on and the head is rebased onto it, the newer push merge base wins over
    # the older fork point, so a failure the push branch got meanwhile is pre-existing, as before.
    from ttp import cli, worktree
    p, run, git, commit, tid, tip = _stacked_repo(env, tmp_path, monkeypatch)
    worktree.record_fork_point(Path.cwd(), tid, tip)
    subprocess.run([*git, "checkout", "-qb", "ttp/t1-demo"], check=True)
    commit(_GATE_BAD, "the change")
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", _FAIL])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert f"base {tip[:12]}: the fork point recorded" in out and "new on this head" in out, out
    subprocess.run([*git, "checkout", "-q", "main"], check=True)
    newer = commit(_GATE_BAD, "the push branch moves on")
    monkeypatch.setattr(cli, "_checks_base", lambda *a: newer)
    subprocess.run([*git, "checkout", "-q", "ttp/t1-demo"], check=True)
    subprocess.run([*git, "reset", "-q", "--hard", newer], check=True)
    commit(_GATE_BAD + "\n\ndef helper():\n    return 1\n", "the change, rebased")
    cli.main(["checks", "--", _FAIL])
    out = capsys.readouterr().out
    assert f"base {newer[:12]}: its merge base with the push branch (newer than {tip[:12]}" in out, out
    assert "pre-existing on the base" in out and "LANDING ONLY" in out


def test_ttp_checks_falls_back_to_the_merge_base_when_the_fork_point_was_rebased_away(
        env, tmp_path, monkeypatch, capsys):
    # The recorded fork point is on a branch the head no longer contains: never used as the base; the
    # gate falls back to the head's merge base with the push branch and says why.
    from ttp import cli, worktree
    p, run, git, commit, tid, tip = _stacked_repo(env, tmp_path, monkeypatch)
    subprocess.run([*git, "checkout", "-qb", "merge/old-batch"], check=True)
    gone = commit(_GATE_BAD, "old batch")
    worktree.record_fork_point(Path.cwd(), tid, gone)
    subprocess.run([*git, "checkout", "-qb", "ttp/t1-demo", "main"], check=True)
    commit(_GATE_BAD, "the change, rebased onto the push branch")
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", _FAIL])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert (f"base {tip[:12]}: its merge base with the push branch; its recorded fork point {gone[:12]} "
            "is not an ancestor of the head (rebased away)") in out, out
    assert "new on this head" in out and "tests/test_gate.py::test_x" in out


def test_a_task_worktree_records_its_fork_point_and_a_finished_tasks_is_pruned(env, monkeypatch):
    # worktree.ensure records the commit each task's worktree was made at: the base a plain task starts
    # from, or the head of the local branch a task is stacked on (pr_branch). The sweep prunes the ref of
    # a finished task whose worktree is gone.
    from ttp import worktree
    p = make(env)
    repo = env["repo"]
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    start = subprocess.run([*git, "rev-parse", worktree.resolve_base(p)], capture_output=True, text=True).stdout.strip()
    plain = p.db.add_task("plain", "s", kind="code")
    path, branch = worktree.ensure(p, dict(p.db.task(plain)))
    p.db.update_task(plain, branch=branch)
    assert worktree.fork_point(p.root, plain) == start
    # A task continuing it starts from its head but inherits its fork point: the earlier part's own
    # failures stay new on the continued head.
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "part 1"], check=True)
    cont = p.db.add_task("part 2", "s", kind="code", labels=[f"continues:{plain}"])
    cpath, _ = worktree.ensure(p, dict(p.db.task(cont)))
    part1 = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert subprocess.run(["git", "-C", str(cpath), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() == part1
    assert worktree.fork_point(p.root, cont) == start != part1
    subprocess.run([*git, "branch", "merge/demo-batch", start], check=True)
    tip = subprocess.run([*git, "commit-tree", "-p", start, "-m", "batch", f"{start}^{{tree}}"],
                         capture_output=True, text=True).stdout.strip()
    subprocess.run([*git, "update-ref", "refs/heads/merge/demo-batch", tip], check=True)
    stacked = p.db.add_task("stacked", "s", kind="code", labels=["pr_branch:merge/demo-batch"])
    worktree.ensure(p, dict(p.db.task(stacked)))
    assert worktree.fork_point(p.root, stacked) == tip != start
    worktree.remove(p, plain, force=True)
    p.db.update_task(plain, status="done")
    assert plain in worktree.prune_rebase_starts(p, [])
    assert worktree.fork_point(p.root, plain) == "" and worktree.fork_point(p.root, stacked) == tip


def test_ttp_checks_a_review_task_keeps_the_push_branch_base(env, tmp_path, monkeypatch, capsys):
    # A review runs in the change's worktree: a fork point or pr_branch recorded for it never makes the
    # change's own commits the base. Its gate compares with the push branch, as before.
    from ttp import cli, worktree
    p, run, git, commit, tid, tip = _stacked_repo(env, tmp_path, monkeypatch)
    p.db.update_task(tid, kind="review", labels=json.dumps(["pr_branch:ttp/t1-demo"]))
    subprocess.run([*git, "checkout", "-qb", "ttp/t1-demo"], check=True)
    bad = commit(_GATE_BAD, "the change")
    worktree.record_fork_point(Path.cwd(), tid, bad)
    commit(_GATE_BAD + "\n\ndef helper():\n    return 1\n", "a review fix on top")
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", _FAIL])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert f"base {tip[:12]}: its merge base with the push branch" in out and "new on this head" in out, out


def _quarantine_repo(env, monkeypatch, files: dict):
    # A project pushing to origin/proj whose tip holds `files` ({path: text}); returns (p, repo, other).
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    for path, text in files.items():
        (other / path).parent.mkdir(parents=True, exist_ok=True)
        (other / path).write_text(text)
        _git_out(other, "add", path)
    _git_out(other, "commit", "-qm", "tests")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    return p, repo, other


def _land_fix(p, repo, other, title: str) -> int:
    # A done code task titled `title` whose one commit lands on origin/proj as a rebased copy.
    tid, path, branch = _code_task(p, f"t{len(p.db.q('SELECT id FROM tasks'))}")
    p.db.update_task(tid, title=title)
    _commit(path, f"fix{tid}.txt", "fixed\n")
    _git_out(other, "fetch", "-q", str(path), branch)
    _git_out(other, "cherry-pick", _git_out(path, "rev-parse", "HEAD"))
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    _git_out(repo, "fetch", "-q", "origin")
    return tid


def test_quarantine_lifts_an_entry_whose_fix_landed_and_keeps_one_whose_fix_did_not(env, monkeypatch):
    from ttp import audit, gatejudge
    tests = "def test_fixed():\n    pass\n\n\ndef test_open():\n    pass\n\n\ndef test_again():\n    pass\n"
    p, repo, other = _quarantine_repo(env, monkeypatch, {"tests/test_q.py": tests})
    now = time.time()
    old_head = _git_out(repo, "rev-parse", "origin/proj")
    ids = [f"tests/test_q.py::{n}" for n in ("test_fixed", "test_open", "test_again")]
    gatejudge.quarantine(p.state, ids, {"head": old_head, "check": "pytest", "host": "h", "task": "1"}, now - 60)
    fix = _land_fix(p, repo, other, "make test_fixed and test_again deterministic")
    # A work task naming the test, and a code task naming it whose commit never landed, lift nothing.
    p.db.add_task("look at test_open", "s", kind="work")
    # A landed code task whose spec only mentions the test is not its fix: only its title counts.
    mention = _land_fix(p, repo, other, "speed up the overview")
    p.db.update_task(mention, spec="see tests/test_q.py::test_open for the slow overview")
    tid, path, _ = _code_task(p, "unlanded")
    p.db.update_task(tid, title="fix test_open")
    _commit(path, "open.txt", "x\n")
    # test_again flaked again at a head that already held the fix: the fix did not fix it.
    gatejudge.quarantine(p.state, [ids[2]], {"head": _git_out(repo, "rev-parse", "origin/proj"), "check": "pytest",
                                             "host": "h", "task": "2"}, now - 30)
    tip = _git_out(repo, "rev-parse", "origin/proj")
    (got,) = gatejudge.lift_due(p, now)
    assert (got["id"], got["commit"]) == (ids[0], tip) and f"its fix #{fix} landed" in got["why"]
    assert [e["id"] for e in gatejudge.lifts(p.state, 0)] == [ids[0]]
    assert sorted(gatejudge.active(p.state, now)) == [ids[2], ids[1]]
    assert gatejudge.lift_due(p, now) == []   # nothing more to lift
    lines = gatejudge.status_lines(p.state, now)
    assert any(ln.startswith("quarantine lifted") and ids[0] in ln and f"#{fix}" in ln for ln in lines)
    assert any("tests lifted from the flaky-test quarantine: 1" in ln and ids[0] in ln
               for ln in audit.lines(p.db, now=now, p=p))


def test_quarantine_never_lifts_an_id_not_found_at_the_tip_and_lists_it_for_a_hand_lift(env, monkeypatch, capsys):
    # A test only on the change branch and an id relative to a rootdir in a subfolder read as "missing"
    # at the push tip exactly like a removed test: none is lifted, all are marked for a hand lift.
    from ttp import audit, cli, gatejudge
    files = {"tests/test_q.py": ("import pytest\n\n\n@pytest.mark.parametrize('x', [1, 2])\n"
                                 "def test_now_param(x):\n    pass\n\n\ndef test_plain():\n    pass\n\n\n"
                                 "class TestK:\n    def test_m(self):\n        pass\n"),
             "tests/cases/area_cases.py": "def test_in_case():\n    pass\n"}
    p, repo, other = _quarantine_repo(env, monkeypatch, files)
    _commit(repo, "test_branch_only.py", "def test_branch_only():\n    pass\n")   # on the change branch only
    now = time.time()
    found = ["tests/test_q.py::test_plain", "tests/test_q.py::test_now_param", "tests/test_q.py::test_now_param[1]",
             "tests/test_q.py::TestK::test_m", "tests/test_q.py::test_in_case"]
    missing = ["test_branch_only.py::test_branch_only", "cases/area_cases.py::test_in_case",
               "tests/test_q.py::test_removed", "tests/test_old.py::test_x"]
    gatejudge.quarantine(p.state, found + missing, {"head": _git_out(repo, "rev-parse", "HEAD"), "check": "pytest",
                                                    "host": "h", "task": ""}, now - 60)
    assert gatejudge.lift_due(p, now) == []
    assert sorted(gatejudge.active(p.state, now)) == sorted(found + missing)
    assert gatejudge.lifts(p.state, 0) == []
    assert gatejudge.not_found(p, found + missing) == set(missing)
    cli.main(["quarantine", "list"])
    out = capsys.readouterr().out.splitlines()
    for i in found + missing:
        (ln,) = [x for x in out if x.startswith(f"quarantined test {i}:")]
        assert ("not found at the push-branch tip; lift by hand" in ln) == (i in missing), ln
    (ln,) = [x for x in audit.lines(p.db, now=now, p=p) if "not found at the push-branch tip" in x]
    assert ln.startswith(f"quarantined tests not found at the push-branch tip; lift by hand if the test was removed: "
                         f"{len(missing)}: ") and all(i in ln for i in missing)
    # No readable push branch: nothing is marked.
    p.set_config("delivery.push_branch", "origin/nowhere")
    assert gatejudge.not_found(p, missing) == set()


def test_quarantine_lift_interrupted_mid_write_leaves_the_old_file_whole(tmp_path, monkeypatch):
    from ttp import gatejudge
    now = time.time()
    gatejudge.quarantine(tmp_path, ["t::a", "t::b"], {"head": "h"}, now)
    path = tmp_path / gatejudge.QUARANTINE_FILE
    before = path.read_text()

    def cut(*a):
        raise OSError("power cut")
    monkeypatch.setattr(os, "replace", cut)
    with pytest.raises(OSError):
        gatejudge.lift(tmp_path, ["t::a"], "fixed", now=now)
    assert path.read_text() == before and sorted(gatejudge.active(tmp_path, now)) == ["t::a", "t::b"]
    assert gatejudge.lifts(tmp_path, 0) == []   # a lift that did not happen is never logged
    assert not [f for f in tmp_path.iterdir() if f.name.endswith(".tmp")]


def test_ttp_quarantine_lists_and_lifts_by_hand(env, monkeypatch, capsys):
    from ttp import cli, gatejudge
    p = make(env)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.delenv("TTP_TASK", raising=False)
    gatejudge.quarantine(p.state, ["tests/test_a.py::test_x"], {"head": "h"}, time.time())

    def ttp(*args):
        try:
            cli.main(["quarantine", *args])
        except SystemExit as e:
            return e.code
        return 0
    assert ttp("list") == 0
    assert "quarantined test tests/test_a.py::test_x" in capsys.readouterr().out
    assert ttp("lift", "tests/test_a.py::test_x") == 2   # no --why
    assert ttp("lift", "tests/test_a.py::test_y", "--why", "gone") == 1
    assert gatejudge.active(p.state)
    assert ttp("lift", "tests/test_a.py::test_x", "--why", "its fix landed") == 0
    assert "lifted tests/test_a.py::test_x" in capsys.readouterr().out
    assert gatejudge.active(p.state) == {}
    (e,) = gatejudge.lifts(p.state, 0)
    assert (e["id"], e["why"], e["by"]) == ("tests/test_a.py::test_x", "its fix landed", "hand")
    assert ttp("list") == 0
    out = capsys.readouterr().out
    assert "quarantine lifted" in out and "its fix landed" in out and "quarantined test" not in out


def _tb(i: str, line: str, at: str, src: str = "assert r.returncode == 0") -> str:
    # pytest's FAILURES block for id `i`: the failing statement, its first `E` line and its location.
    name = i.rpartition("::")[2]
    return f"_____ {name} _____\n\n    def {name}():\n>       {src}\nE       {line}\n\n{at}: AssertionError\n"


def _fails(blocks: dict) -> tuple[int, str]:
    # A rerun exiting 1 with a FAILURES block and a summary line per failing id.
    out = "=== FAILURES ===\n" + "".join(_tb(i, *b) for i, b in blocks.items()) + "=== short test summary info ===\n"
    out += "".join(f"FAILED {i} - AssertionError: {b[0]}\n" for i, b in blocks.items())
    return 1, out + f"=== {len(blocks)} failed in 0.1s ===\n"


def test_gate_judge_calls_a_failure_pre_existing_only_when_base_and_head_fail_the_same_way(tmp_path):
    from ttp import gatejudge
    a = "tests/test_a.py::test_pre"
    same = ("assert 1 == 0", "tests/test_a.py:1368")
    # (b) The same assert, on the base at another line (the change moved it) and in another checkout: not blocking.
    v = gatejudge.judge([a], lambda at, ids: _fails({a: same if at == "head" else ("assert 1 == 0", "/tmp/co9/tests/test_a.py:1360")}),
                        tmp_path / "b", "b1", "host-a", 1000.0)
    assert v["pre_existing"] == [a] and not gatejudge.blocks(v)
    # (c) Another first error line on the base (the rerun's nested pytest exit 5 there, 1 on the head): blocking,
    # never proven new (the base failed it too), and said so.
    for n, base_fail in enumerate((("assert 5 == 0", "tests/test_a.py:1368"), ("KeyError: 'x'", "tests/test_a.py:1384"),
                                   # the same text from another assert (another statement failed)
                                   ("assert 1 == 0", "tests/test_a.py:1384", "assert out.returncode == 0"))):
        v = gatejudge.judge([a], lambda at, ids: _fails({a: same if at == "head" else base_fail}),
                            tmp_path / f"c{n}", "b1", "host-a", 1000.0)
        assert v["new"] == [a] and v["pre_existing"] == [] and v["other_failure"] == [a] and gatejudge.blocks(v)
        assert gatejudge.proven(v) == []
        assert "another way" in " ".join(gatejudge.lines(v))
    # The saved base failure keeps its mark: a later judge compares with it without a rerun, both ways.
    calls = []

    def head_only(at, ids):
        calls.append(at)
        return _fails({a: same}) if at == "head" else (None, "")
    assert gatejudge.judge([a], head_only, tmp_path / "b", "b1", "host-a", 1001.0)["pre_existing"] == [a]
    assert gatejudge.judge([a], head_only, tmp_path / "c0", "b1", "host-a", 1001.0)["other_failure"] == [a]
    assert calls == ["head", "head"]
    # A base failure saved without a mark (before marks were kept) proves nothing: the base runs again.
    gatejudge.save_base(tmp_path / "old", "host-a", "b1", {a: True}, 1000.0)
    calls.clear()
    v = gatejudge.judge([a], head_only, tmp_path / "old", "b1", "host-a", 1001.0)
    assert calls == ["head", "base"] and v["new"] == [a] and v["pre_existing"] == []
    # Marks that share nothing (a base rerun showing neither a traceback nor a summary message) prove nothing.
    assert not gatejudge.same_failure({"line": "assert 0"}, {"at": "test_a.py:3: AssertionError"})
    assert not gatejudge.same_failure({}, {})
    assert not gatejudge.same_failure({"msg": "x", "src": "f()"}, {"msg": "x", "src": "g()"})
    assert gatejudge.same_failure({"msg": "x", "src": "f()"}, {"msg": "x"})
    # Temp folders and addresses are not a difference; the numbers of an assert are.
    m = gatejudge.marks(_fails({a: ("assert '/tmp/pytest-of-u/pytest-41/t0' at 0x7f01", "t.py:3")})[1], [a])[a]
    m2 = gatejudge.marks(_fails({a: ("assert '/tmp/pytest-of-u/pytest-42/t0' at 0x7f99", "t.py:3")})[1], [a])[a]
    assert gatejudge.same_failure(m, m2) and m["at"] == "t.py:3: AssertionError"
    # The random part of a temp name (mkdtemp's, mktemp's) is not a difference either: one failure on the
    # head and the base of a spill host differs only by it.
    for x, y in (("homes/home-y7h9wyh0", "homes/home-w56a_m92"), ("homes/home-biknwoxz", "homes/home-q3k_0zz1"),
                 ("/tmp/ttp-check.T6w5cg3S/out", "/tmp/ttp-check.a8Zq01bX/out")):
        m = gatejudge.marks(_fails({a: (f"AssertionError: '/tmp/pytest-of-u/pytest-350/t0/{x}' left", "t.py:3")})[1], [a])[a]
        m2 = gatejudge.marks(_fails({a: (f"AssertionError: '/tmp/pytest-of-u/pytest-351/t0/{y}' left", "t.py:3")})[1], [a])[a]
        assert gatejudge.same_failure(m, m2), (m, m2)
    # Ordinary path words are not a temp name's random part: unproven sameness never excuses a failure.
    for x, y in (("plugins/tt-project/x", "plugins/tt-review/x"),
                 ("refs/heads/user/tt-project", "refs/heads/user/tt-dogfood"),
                 ("out/report-summary", "out/report-details"), ("state/task.status", "state/task.summary"),
                 ("out/run-complete", "out/run-finished"), ("homes/home-y7h9wyh0", "homes/home-y7h9wyh01"),
                 ("homes/home-y7h9wyh0", "homes/user-w56a_m92")):
        m = gatejudge.marks(_fails({a: (f"AssertionError: '/tmp/pytest-of-u/pytest-350/t0/{x}' left", "t.py:3")})[1], [a])[a]
        m2 = gatejudge.marks(_fails({a: (f"AssertionError: '/tmp/pytest-of-u/pytest-351/t0/{y}' left", "t.py:3")})[1], [a])[a]
        assert not gatejudge.same_failure(m, m2), (m, m2)
    m = gatejudge.marks(_fails({a: ("AssertionError: '/tmp/pytest-of-u/pytest-350/t0/homes/home-y7h9wyh0' left", "t.py:3")})[1], [a])[a]
    m2 = gatejudge.marks(_fails({a: ("AssertionError: '/tmp/pytest-of-u/pytest-351/t0/homes/home-y7h9wyh0' gone", "t.py:3")})[1], [a])[a]
    assert not gatejudge.same_failure(m, m2)
    # A base rerun that errors (exit 4) or selects nothing (exit 5, a test the base does not have) never
    # makes the failure pre-existing.
    for n, rc in enumerate((4, 5)):
        v = gatejudge.judge([a], lambda at, ids: _fails({a: same}) if at == "head" else (rc, "no tests ran\n"),
                            tmp_path / f"rc{n}", "b1", "host-a", 1000.0)
        assert v["new"] == [a] and v["pre_existing"] == [] and gatejudge.blocks(v)



def test_gate_judge_same_failure_ignores_a_venv_path_and_reads_long_pytest_headers(tmp_path):
    """#7330: the same assertion on head and base differed only by the checks env folder in a CompletedProcess
    repr, and pytest's header of a long test name has one '_' a side, so its block was not read and the
    saved base mark was empty: the failure was judged 'another way' and blocked. Now both are the same."""
    from ttp import gatejudge, sharedchecks
    a = "tests/test_a.py::test_" + "a_very_long_name" * 6
    name = a.rpartition("::")[2]

    def run(env, pid, secs):
        line = (f"AssertionError: CompletedProcess(args=['/opt/ci/.tt-project/checks-env/{env}/bin/python3'], "
                f"pid {pid}, took {secs}s)")
        out = (f"=== FAILURES ===\n_ {name} _\n\n    def {name}():\n        helper()\n\n"
               f"tests/test_a.py:3: \n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _\n\n"
               f"    def helper():\n>       assert r.returncode == 0, r\nE       {line}\n\n"
               f"tests/test_a.py:9: AssertionError\n=== short test summary info ===\nFAILED {a}\n=== 1 failed in 0.1s ===\n")
        return 1, out
    head, base = run("0c594e2e7d0528a0.d8e44f94", 4101, "1.52"), run("9ab31f07c2d4e5a6.0f1e2d3c", 77, "0.98")
    block = sharedchecks.failure_blocks(head[1], [a])[a]
    assert "E       AssertionError" in block, "the block runs past the frame separator to the failing frame"
    m, m2 = gatejudge.marks(head[1], [a])[a], gatejudge.marks(base[1], [a])[a]
    assert m["line"] and m["src"] == "assert r.returncode == 0, r" and gatejudge.same_failure(m, m2), (m, m2)
    v = gatejudge.judge([a], lambda at, ids: head if at == "head" else base, tmp_path / "s", "b1", "host-a", 1000.0)
    assert v["pre_existing"] == [a] and not gatejudge.blocks(v), v
    # Another assertion still differs; a word made of hex letters or a plain number is not a hash.
    assert not gatejudge.same_failure(m, gatejudge.marks(run("0c594e2e7d0528a0", 1, "1")[1].replace(
        "returncode == 0", "returncode == 2"), [a])[a])
    assert gatejudge._mark_norm("decade facade 123456") == "decade facade 123456"
    # A base failure saved with an empty mark (an output its parser could not read) proves nothing: the base reruns.
    gatejudge.save_base(tmp_path / "e", "host-a", "b1", {a: True}, 1000.0, {a: {"line": "", "msg": "", "src": "", "at": ""}})
    calls = []
    v = gatejudge.judge([a], lambda at, ids: calls.append(at) or (head if at == "head" else base),
                        tmp_path / "e", "b1", "host-a", 1001.0)
    assert calls == ["head", "base"] and v["pre_existing"] == [a], (calls, v)


def _narrow_fails(i: str, line: str) -> tuple[int, str]:
    # A rerun over ssh with no tty (80 columns): a test name wider than that gets one `_` on each side, the
    # traceback has a `_ _ _` frame separator and the summary line has no room for the message.
    name = i.rpartition("::")[2]
    out = (f"{'=' * 30} FAILURES {'=' * 30}\n_ {name} _\n\n    def {name}():\n>       helper()\n\nt.py:9: \n"
           f"{'_ ' * 40}\n\n    def helper():\n>       assert r.returncode == 0\nE       {line}\n\n"
           f"t.py:3: AssertionError\n{'=' * 20} short test summary info {'=' * 20}\nFAILED {i}\n")
    return 1, out + "=== 1 failed in 0.1s ===\n"


def test_gate_judge_reads_a_narrow_terminals_failure_and_never_reuses_a_saved_base_mark_it_could_not_read(tmp_path):
    from ttp import gatejudge, sharedchecks
    a = "tests/test_a.py::test_" + "a_name_wider_than_an_ssh_terminal_of_eighty_columns_" * 2
    m = gatejudge.marks(_narrow_fails(a, "assert 1 == 0")[1], [a])[a]
    assert m["line"] == "assert 1 == 0" and m["src"] == "assert r.returncode == 0" and m["at"] == "t.py:3: AssertionError"
    assert "def helper" in sharedchecks.failure_blocks(_narrow_fails(a, "assert 1 == 0")[1], [a])[a]
    # Base and head failing the same way on a spill host: pre-existing, not "another way".
    calls = []

    def run(at, ids):
        calls.append(at)
        return _narrow_fails(a, "assert 1 == 0")
    v = gatejudge.judge([a], run, tmp_path / "s", "b1", "host-a", 1000.0)
    assert v["pre_existing"] == [a] and not gatejudge.blocks(v)
    # A base failure saved with a mark that shows nothing (from output marks() could not read) proves
    # nothing: the base runs again instead of the empty mark calling every failure another way for days.
    gatejudge.save_base(tmp_path / "e", "host-a", "b1", {a: True}, 1000.0, {a: {"line": "", "msg": "", "src": "", "at": ""}})
    calls.clear()
    v = gatejudge.judge([a], run, tmp_path / "e", "b1", "host-a", 1001.0)
    assert calls == ["head", "base"] and v["pre_existing"] == [a] and v["other_failure"] == []


_NESTED = '''import json, os, subprocess, sys


def test_nested(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\\n")
    (tmp_path / "test_inner.py").write_text("def test_inner():\\n    {body}\\n")
    (tmp_path / "test_more.py").write_text("def test_more():\\n    pass\\n")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
                       cwd=str(tmp_path), capture_output=True, text=True)
    seen = {k: v for k, v in os.environ.items() if k.startswith(("TTP_PYTEST_SELECT", "PYTEST_ADDOPTS"))}
    with open(os.environ["NESTED_ENV_OUT"], "a") as f:
        f.write(json.dumps({"env": seen, "rc": r.returncode, "out": r.stdout[-300:]}) + "\\n")
    assert r.returncode == 0
'''


def test_gate_rerun_selection_never_reaches_a_pytest_the_test_starts(tmp_path):
    """(d) The rerun's selection (the select plugin's variables and its -p load) stays in the rerun's own
    pytest: a pytest a test starts runs its own tests, all of them."""
    from ttp import gatejudge
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "pytest.ini").write_text("[pytest]\n")
    (proj / "tests" / "test_n.py").write_text(_NESTED.replace("{body}", "pass"))
    seen = tmp_path / "seen.jsonl"
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "TTP_PYTEST_"))}
    e = {**clean, **gatejudge.select_env(["tests/test_n.py::test_nested"], clean), "NESTED_ENV_OUT": str(seen)}
    e["PYTEST_ADDOPTS"] = "-p no:randomly " + e["PYTEST_ADDOPTS"]
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--rootdir={proj}"],
                       cwd=str(proj), env=e, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads(seen.read_text())
    assert got["rc"] == 0 and "2 passed" in got["out"], got
    assert got["env"] == {"PYTEST_ADDOPTS": "-p no:randomly"}, got


def test_ttp_checks_blocks_a_nested_pytest_test_new_on_the_head(env, tmp_path, monkeypatch, capsys):
    """(a) A test that starts its own pytest fails on the head and passes on the base. The gate's rerun of it
    must not leak its selection into that pytest (which then selects nothing and fails the test the same way
    on both): it is new on the head and blocks, never pre-existing."""
    from ttp import cli
    monkeypatch.setenv("NESTED_ENV_OUT", str(tmp_path / "seen.jsonl"))
    p, run = _gate_repo(env, tmp_path, monkeypatch, _NESTED.replace("{body}", "pass"),
                        _NESTED.replace("{body}", "assert False"))
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", _FAIL])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "new on this head" in out and "pre-existing" not in out, out
    assert not json.loads((run / "checks.json").read_text())["passed"]


def test_gate_judge_same_failure_ignores_commit_shas_and_pytest_temp_roots(tmp_path):
    """#7561: one failure on head and base (run 15183) differed only by the pytest temp root (a parallel
    head run's worker folder `w1`, the base rerun's none) and the short commit sha a push refusal named,
    so the gate called it 'failed another way' and blocked. Now both are the same; a new failure still blocks."""
    from ttp import gatejudge
    a = "tests/test_a.py::test_leak"
    cmd = ("subprocess.CalledProcessError: Command '['git', '-C', '{root}/test_leak0/leak-repo', 'push']' "
           "returned non-zero exit status 1.")
    head = cmd.format(root="/dev/shm/ttp-1/4f2a/pytest-of-u/pytest-0/w1")
    for root in ("/dev/shm/ttp-1/4f2a/pytest-of-u/pytest-25", "/tmp/pytest-of-u/pytest-3/popen-gw2",
                 "/tmp/pytest-of-u/pytest-current/gw0"):
        v = gatejudge.judge([a], lambda at, ids: _fails({a: (head if at == "head" else cmd.format(root=root), "t.py:3")}),
                            tmp_path / root.replace("/", "_"), "b1", "host-a", 1000.0)
        assert v["pre_existing"] == [a] and not gatejudge.blocks(v), (root, v)
    for x, y in (("commit 3eedf5a", "commit 65cdc06"), ("commit 1234567", "commit 7654321"),
                 ("sha deadbeefcafe", "sha 0123456789ab"), ("tip " + "a" * 40, "tip " + "1" * 40)):
        m = gatejudge.marks(_fails({a: (f"push refused: {x} holds a token", "t.py:3")})[1], [a])[a]
        m2 = gatejudge.marks(_fails({a: (f"push refused: {y} holds a token", "t.py:3")})[1], [a])[a]
        assert gatejudge.same_failure(m, m2), (m, m2)
    # A real new failure still blocks: another exit status, another file in the temp folder, another word.
    for other in (head.replace("status 1", "status 128"), head.replace("leak-repo", "clean-repo"),
                  head.replace("push", "fetch")):
        v = gatejudge.judge([a], lambda at, ids: _fails({a: (head if at == "head" else other, "t.py:3")}),
                            tmp_path / str(hash(other)), "b1", "host-a", 1000.0)
        assert v["other_failure"] == [a] and gatejudge.blocks(v), (other, v)
    # Plain numbers and words stay: an assert's count is not a sha.
    assert gatejudge._mark_norm("assert 1234567 == 7654321") == "assert 1234567 == 7654321"
    assert gatejudge._mark_norm("effaced decade") == "effaced decade"
    assert gatejudge._mark_norm("stopped at 1000000 bytes") != gatejudge._mark_norm("stopped at 2000000 bytes")
