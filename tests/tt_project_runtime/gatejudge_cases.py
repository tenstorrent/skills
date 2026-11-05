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
    assert v["new"] == [a]


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
