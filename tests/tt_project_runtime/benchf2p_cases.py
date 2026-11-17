"""Fail-to-pass test writing and selection (benchf2p) cases."""
from __future__ import annotations

import subprocess
import sys

import pytest


def _f2p_repo(tmp_path, cap: str, fix: str) -> tuple:
    """A two-commit git repo: base then head, mymod.thing going 1 -> 2."""
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    (d / "mymod.py").write_text(cap)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], check=True)
    (d / "mymod.py").write_text(fix)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "head"], check=True)
    return d


CAP, FIX = "def thing():\n    return 1\n", "def thing():\n    return 2\n"
GOOD = ("import sys\nsys.path.insert(0, '.')\nimport mymod\n"
        "def test_thing():\n    assert mymod.thing() == 2, 'fail on base, pass on head'\n"
        "def test_wrong():\n    assert mymod.thing() == 99, 'fail everywhere'\n")


def test_f2p_select_keeps_only_tests_that_fail_on_base_and_pass_on_head(tmp_path):
    from ttp import benchf2p
    repo = _f2p_repo(tmp_path, CAP, FIX)
    got = benchf2p.select(repo, "HEAD~1", "HEAD", GOOD, cmd=sys.executable + " -m pytest")
    assert got["fault"] is False
    assert got["kept"] == ["f2p_test_module.py::test_thing"], got
    assert got["why"] == "kept the tests that fail on the base tree and pass on the change"


def test_f2p_select_with_the_default_quiet_command_still_reads_each_test(tmp_path):
    # cells: f2p-default-cmd
    # bench.TEST_CMD carries -q, which cancels select's -v: the per-test lines vanished and every task
    # read "collected no test". Its own interpreter stands in for python3; the flags stay as shipped.
    from ttp import bench, benchf2p
    assert " -q " in bench.TEST_CMD + " "
    repo = _f2p_repo(tmp_path, CAP, FIX)
    got = benchf2p.select(repo, "HEAD~1", "HEAD", GOOD, cmd=bench.TEST_CMD.replace("python3", sys.executable, 1))
    assert got["fault"] is False and got["kept"] == ["f2p_test_module.py::test_thing"], got


def test_f2p_select_drops_a_module_that_passes_everywhere_or_nowhere(tmp_path):
    from ttp import benchf2p
    repo = _f2p_repo(tmp_path, CAP, FIX)
    triv = "def test_always():\n    assert True\n"
    got = benchf2p.select(repo, "HEAD~1", "HEAD", triv, cmd=sys.executable + " -m pytest")
    assert got["kept"] == [] and got["fault"] is False
    got = benchf2p.select(repo, "HEAD", "HEAD", GOOD, cmd=sys.executable + " -m pytest")
    assert got["kept"] == [] and got["fault"] is False


def test_f2p_select_faults_on_a_module_that_does_not_parse_or_collect(tmp_path):
    from ttp import benchf2p
    repo = _f2p_repo(tmp_path, CAP, FIX)
    bad = "def test_x(:\n"
    got = benchf2p.select(repo, "HEAD~1", "HEAD", bad, cmd=sys.executable + " -m pytest")
    assert got["kept"] == [] and got["fault"] is True
    assert "does not parse" in got["why"]
    empty = "X = 1\n"
    got = benchf2p.select(repo, "HEAD~1", "HEAD", empty, cmd=sys.executable + " -m pytest")
    assert got["kept"] == [] and got["fault"] is True


def test_f2p_parse_and_write_tests(tmp_path):
    from ttp import benchf2p
    assert benchf2p.parse("def test_a():\n    assert True\n") is True
    assert benchf2p.parse("def broken(:\n") is False
    asks = []
    text = benchf2p.write_tests({"spec": "S"}, lambda p: (asks.append(p), "MOD")[1])
    assert asks == [benchf2p.WRITER_PROMPT.format(spec="S")]
    assert text == "MOD"
