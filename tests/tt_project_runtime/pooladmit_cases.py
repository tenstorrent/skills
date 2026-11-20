# Test-pool admission tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new `ttp pool-admit` tests here.
from __future__ import annotations


def _pool_check(monkeypatch, tmp_path, limit_s=900, ran_s=0, waits=None):
    """A push check of `limit_s` that has run `ran_s` s so far, with its own home for the pool's history."""
    from ttp import checkwait, pooladmit, project
    monkeypatch.setattr(project, "HOME_DIR", tmp_path / "home")
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "proj"))
    monkeypatch.delenv(pooladmit.WALL_ENV, raising=False)
    monkeypatch.setenv(pooladmit.LIMIT_ENV, f"{time.time() - ran_s:.0f}:{limit_s}")
    if waits is None:
        monkeypatch.delenv(checkwait.ENV, raising=False)
    else:
        monkeypatch.setenv(checkwait.ENV, str(waits))


def test_pool_admit_refuses_a_cut_run_its_granted_slots_cannot_finish_inside_the_check_limit(
        tmp_path, monkeypatch, capsys):
    """6 shards planned, 2 granted, a 1000 s limit: a passing 600 s run of 122 files on 6 slots puts the run
    at about 1800 s on 2, so it is not started there (exit 3) and stdout names the 4 slots that would fit;
    granted those 4 it starts. A run recorded with --ran leaves out its own pool waits."""
    from ttp import checkwait, locks, pooladmit
    _pool_check(monkeypatch, tmp_path, limit_s=1000)
    assert _ttp("pool-admit", "--what", "cpu", "--ran", "600", "--granted", "6", "--planned", "6",
                "--files", "122") == 0
    capsys.readouterr()
    assert _ttp("pool-admit", "--what", "cpu", "--planned", "6", "--granted", "2", "--files", "122") == pooladmit.SHORT
    out = capsys.readouterr()
    assert out.out.strip() == "4", out
    assert "2 of 6 planned slots would take about 1800 s" in out.err and "per file" in out.err, out.err
    assert "not started on them" in out.err
    assert _ttp("pool-admit", "--what", "cpu", "--planned", "6", "--granted", "4", "--files", "122") == 0
    assert capsys.readouterr().out == ""
    # without --files, the per-shard median says the same
    assert _ttp("pool-admit", "--what", "cpu", "--planned", "6", "--granted", "2") == pooladmit.SHORT
    assert "per planned shard" in capsys.readouterr().err
    # time already run counts: 500 s into the limit, 4 slots (about 900 s) no longer fit, 8 would
    _pool_check(monkeypatch, tmp_path, limit_s=1000, ran_s=500)
    assert _ttp("pool-admit", "--what", "cpu", "--planned", "6", "--granted", "4", "--files", "122") == pooladmit.SHORT
    assert capsys.readouterr().out.strip() == "8"
    # its waits for a grant do not, as in the push: 500 s of them leave the whole limit
    waits = tmp_path / "waits"
    waits.mkdir()
    locks.record_wait(waits, "lock", time.time() - 500, time.time())
    _pool_check(monkeypatch, tmp_path, limit_s=1000, ran_s=500, waits=waits)
    assert _ttp("pool-admit", "--what", "cpu", "--planned", "6", "--granted", "4", "--files", "122") == 0
    # nor do its waits for a pool slot, as in the push: 500 s of them leave the whole limit
    pooled = tmp_path / "waits-pool"
    (pooled / checkwait.POOL).mkdir(parents=True)
    locks.record_wait(pooled / checkwait.POOL, "shard", time.time() - 500, time.time())
    _pool_check(monkeypatch, tmp_path, limit_s=1000, ran_s=500, waits=pooled)
    assert _ttp("pool-admit", "--what", "cpu", "--planned", "6", "--granted", "4", "--files", "122") == 0
    # a run's own pool waits are not its run time: 1000 s with 400 s waiting records 600 s
    other = tmp_path / "waits2"
    (other / checkwait.POOL).mkdir(parents=True)
    locks.record_wait(other / checkwait.POOL, "shard", time.time() - 500, time.time() - 100)
    _pool_check(monkeypatch, tmp_path, waits=other)
    assert _ttp("pool-admit", "--what", "other", "--ran", "1000", "--granted", "2", "--files", "100") == 0
    assert pooladmit.estimate("other", 2, 2, 900, files=100)[0] == pytest.approx(600, abs=2)
    # no time left at all: refused, with no slot count that would fit
    _pool_check(monkeypatch, tmp_path, ran_s=1000)
    assert _ttp("pool-admit", "--what", "cpu", "--planned", "6", "--granted", "5") == pooladmit.SHORT
    out = capsys.readouterr()
    assert out.out == "" and "no time is left" in out.err, out


def test_pool_admit_leaves_a_run_granted_its_planned_slots_or_with_no_limit_unchanged(tmp_path, monkeypatch, capsys):
    """Granted every planned slot (or more), the run starts whatever its history says, saying nothing;
    outside a push check or checks run with a time limit, a cut run starts as before; a cut run whose
    estimate fits starts. Bad numbers are refused (exit 2)."""
    from ttp import pooladmit
    _pool_check(monkeypatch, tmp_path)
    pooladmit.record("cpu", 5000, granted=6, planned=6, files=122)   # far over any limit
    for granted in ("6", "8"):
        assert _ttp("pool-admit", "--what", "cpu", "--planned", "6", "--granted", granted, "--files", "122") == 0
        out = capsys.readouterr()
        assert out.out == "" and out.err == "", out
    monkeypatch.delenv(pooladmit.LIMIT_ENV)
    assert _ttp("pool-admit", "--what", "cpu", "--planned", "6", "--granted", "1") == 0
    for junk in ("abc", "1:nan", f"{time.time():.0f}:-5", f"{time.time():.0f}:0", ":900"):   # no limit known
        assert pooladmit.time_left(env={pooladmit.LIMIT_ENV: junk}) is None, junk
    _pool_check(monkeypatch, tmp_path)
    pooladmit.record("fast", 120, granted=6, planned=6, files=122)
    assert _ttp("pool-admit", "--what", "fast", "--planned", "6", "--granted", "2", "--files", "122") == 0
    for bad in (("--planned", "6"), ("--planned", "0", "--granted", "2"), ("--planned", "6", "--granted", "x"),
                ("--granted", "2"), ("--granted", "2", "--ran", "-1"), ("--granted", "2", "--ran", "nan")):
        assert _ttp("pool-admit", *bad) == 2, bad
    assert pooladmit._load().get("proj/cpu") and len(pooladmit._load()) == 2   # refused calls record nothing


def test_pool_admit_with_no_timing_history_uses_the_conservative_estimate(tmp_path, monkeypatch, capsys):
    """With no history, each planned shard is taken as half the whole limit: 2 of 6 slots (1350 s on a
    900 s limit, 100 s of it run) are refused, naming the 4 slots that fit, and 4 of 6 (675 s) start. A
    `ttp checks` wall cap counts too, the tighter limit winning; history kept for another pool or project
    is not this one's."""
    from ttp import pooladmit
    _pool_check(monkeypatch, tmp_path, ran_s=100)
    pooladmit.record("other-pool", 10, granted=6, planned=6, files=122)
    assert _ttp("pool-admit", "--planned", "6", "--granted", "2", "--files", "122") == pooladmit.SHORT
    out = capsys.readouterr()
    assert "about 1350 s (no timing history" in out.err and out.out.strip() == "4", out
    assert _ttp("pool-admit", "--planned", "6", "--granted", "4", "--files", "122") == 0
    assert pooladmit.estimate("", 6, 4, 900.0)[0] == 675.0
    # a checks run's 600 s wall cap, 100 s gone, is tighter than the push check's 900 s
    monkeypatch.setenv(pooladmit.WALL_ENV, f"{time.time() - 100:.0f}:600")
    left, limit = pooladmit.time_left()
    assert 495 <= left <= 501 and limit == 600, (left, limit)
    assert _ttp("pool-admit", "--planned", "6", "--granted", "4") == 0                 # 300 s x 6 / 4: 450 s
    assert _ttp("pool-admit", "--planned", "6", "--granted", "3") == pooladmit.SHORT   # 600 s, over 500 s
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "another"))
    monkeypatch.delenv(pooladmit.WALL_ENV)
    pooladmit.record("", 10, granted=6, planned=6)
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "proj"))
    assert "no timing history" in pooladmit.estimate("", 6, 2, 900.0)[1]
    # a corrupt history, or one of junk rows, is no history
    for junk in ("{not json", '["a"]', '{"proj/": [1, {"per_file": "nan", "per_shard": -3}]}'):
        (tmp_path / "home" / "check-slots" / pooladmit.HISTORY).write_text(junk)
        assert "no timing history" in pooladmit.estimate("", 6, 2, 900.0, files=5)[1], junk
    pooladmit.record("", 60, granted=6, planned=6)   # and a run recorded over it starts a new one
    assert "per planned shard" in pooladmit.estimate("", 6, 2, 900.0)[1]


def test_push_and_checks_give_each_check_its_time_limit_for_pool_admission(tmp_path, monkeypatch):
    """A push check's environment carries its start and run-time limit (pooladmit.LIMIT_ENV), so a pool
    script inside it can ask `ttp pool-admit`; none with no limit."""
    from ttp import pooladmit, push
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "env.txt"
    cmd = f"{sys.executable} -c \"import os; open({str(out)!r}, 'w').write(os.environ.get({pooladmit.LIMIT_ENV!r}, ''))\""
    for limit, want in ((900.0, ":900"), (None, "")):
        monkeypatch.setattr(push, "check_limit_s", limit)
        monkeypatch.setattr(push, "check_queue_s", 60.0)
        monkeypatch.setattr(push, "pool_wait_s", None)
        assert push._run_one(cmd, repo, dict(os.environ), lambda s: None) == 0
        got = out.read_text()
        assert got.endswith(want) and (not want or abs(float(got.split(":")[0]) - time.time()) < 60), got
