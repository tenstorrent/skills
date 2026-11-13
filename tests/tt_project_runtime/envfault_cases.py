# Environment fault tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new envfault.py tests here, not there.
from __future__ import annotations

_OLD_PYTEST = {"sig": "env:old-runner:pytest", "kind": "module", "python": "/usr/bin/python3", "runner": "pytest",
               "module": "pytest", "version": "6.2.5", "min": "7.3", "cmd": "python3 -m pytest -q"}


def _scratch_cache(env, tmp_path, monkeypatch):
    """A scratch HOME and cache: pip's selfcheck folder the test fills, never the account's."""
    p = make(env)
    monkeypatch.setenv("HOME", str(tmp_path / "scratch-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "scratch-home" / ".cache"))
    monkeypatch.delenv("PIP_CACHE_DIR", raising=False)
    return p, tmp_path / "scratch-home" / ".cache" / "pip" / "selfcheck"


def _pip_ran(folder, key, ts):
    from datetime import datetime, timezone
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / hashlib.sha224(key.encode()).hexdigest()
    f.write_text(json.dumps({"key": key, "last_check": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                             "pypi_version": "26.0"}))
    os.utime(f, (ts, ts))


def _fault_text(p):
    rows = p.db.q("SELECT text FROM events WHERE kind='observation' AND fingerprint LIKE 'env:%'")
    assert len(rows) == 1, rows
    return rows[0]["text"]


def test_a_runner_fault_names_the_pip_run_and_the_detached_job_that_likely_caused_it(env, tmp_path, monkeypatch):
    """An old pytest's observation names the pip runs of the 10 minutes before (pip's selfcheck cache)
    and the detached job, of any project in the account's registry, whose log names that interpreter.
    Older pip runs and jobs that ended before the window or name no such prefix are left out."""
    from ttp import envfault
    from ttp.project import FOLDER, register
    p, sc = _scratch_cache(env, tmp_path, monkeypatch)
    now = time.time()
    hit, old = str(tmp_path / "standalone" / "python"), str(tmp_path / "stale-venv")
    _pip_ran(sc, hit, now - 120)
    _pip_ran(sc, old, now - 3600)
    (sc / "garbage").write_text("{not json")   # skipped, not fatal
    other = tmp_path / "other"
    run = other / FOLDER / "state" / "runs" / "41"
    run.mkdir(parents=True)
    logs = {n: run / f"{n}.log" for n in ("lock", "quiet", "early")}
    logs["lock"].write_text(f"+ {hit}/bin/python -m pip install pytest==9.0\nok\n")
    logs["quiet"].write_text("nothing here\n")
    logs["early"].write_text(f"{hit}/bin/python ran too\n")
    (run / "detached.json").write_text(json.dumps([
        {"name": "lock", "log": str(logs["lock"]), "task": 77, "run": 41, "started": now - 200, "ended": now - 90},
        {"name": "quiet", "log": str(logs["quiet"]), "started": now - 200},
        {"name": "early", "log": str(logs["early"]), "started": now - 7200, "ended": now - 3600}]))
    register("other", {"dir": str(other)})
    assert envfault.record(p, dict(_OLD_PYTEST), "9")
    text = _fault_text(p)
    assert f"Likely cause: pip ran in the 10 minutes before, from {hit} at " in text, text
    assert old not in text and "Detached job lock of other (task #77, run 41)" in text, text
    assert str(logs["lock"]) in text and "quiet" not in text and "early" not in text, text
    assert text.index("Likely cause") < text.index("Waiting tasks"), text
    # The same fault seen again raises nothing more.
    assert not envfault.record(p, dict(_OLD_PYTEST), "10")
    assert len(p.db.q("SELECT 1 FROM events WHERE kind='observation' AND fingerprint LIKE 'env:%'")) == 1


def test_a_runner_fault_with_no_pip_run_before_it_says_none_was_found(env, tmp_path, monkeypatch):
    from ttp import envfault
    p, sc = _scratch_cache(env, tmp_path, monkeypatch)
    assert envfault.cause(p).startswith(" Likely cause: no pip run in the 10 minutes before")   # no cache at all
    _pip_ran(sc, str(tmp_path / "old"), time.time() - 3600)
    envfault.record(p, {**_OLD_PYTEST, "sig": "env:missing-runner:pytest", "version": None})
    text = _fault_text(p)
    assert "Likely cause: no pip run in the 10 minutes before" in text and str(tmp_path / "old") not in text, text
    # A pip run with no tt-project job naming it is said so.
    _pip_ran(sc, str(tmp_path / "new"), time.time() - 30)
    assert "No tt-project detached job's log names those interpreters" in envfault.cause(p)


def test_a_runner_fault_whose_pip_cache_is_unreadable_says_so_and_is_still_recorded(env, tmp_path, monkeypatch):
    from ttp import envfault
    p, sc = _scratch_cache(env, tmp_path, monkeypatch)
    sc.parent.mkdir(parents=True)
    sc.write_text("a file where pip keeps a folder")
    assert envfault.record(p, dict(_OLD_PYTEST))
    text = _fault_text(p)
    assert "Likely cause: not read: pip's selfcheck cache" in text and "unreadable" in text, text
    assert "env:old-runner:pytest" in envfault.open_faults(p)
    # A diagnosis that raises never stops the record; a host fault gets no pip diagnosis.
    monkeypatch.setattr(envfault, "pip_runs", lambda *a, **k: 1 / 0)
    assert envfault.cause(p) == " Likely cause: not read (ZeroDivisionError)."
    assert envfault.record(p, {"sig": "env:check-host:box", "kind": "host", "host": "box", "cmd": "x"})
    rows = p.db.q("SELECT text FROM events WHERE kind='observation' AND fingerprint LIKE 'env:check-host:%'")
    assert rows and "Likely cause" not in rows[0]["text"], rows


def test_the_pip_selfcheck_cache_is_found_where_pip_keeps_it_on_each_platform(tmp_path):
    from ttp import envfault
    h = str(tmp_path)
    assert envfault._selfcheck_dir({"HOME": h}, "linux") == tmp_path / ".cache/pip/selfcheck"
    assert envfault._selfcheck_dir({"HOME": h}, "darwin") == tmp_path / "Library/Caches/pip/selfcheck"
    assert envfault._selfcheck_dir({"HOME": h, "XDG_CACHE_HOME": h + "/x"}, "darwin") == tmp_path / "x/pip/selfcheck"
    assert envfault._selfcheck_dir({"HOME": h, "PIP_CACHE_DIR": h + "/p"}, "darwin") == tmp_path / "p/selfcheck"


def test_env_fault_observation_honours_an_observation_mute_on_its_signature(env):
    # A check host that flaps raises a high `checks` observation per episode; a mute on that source and
    # signature stores it muted and counts it there, so it never wakes the coordinator. Without one it queues.
    from ttp import envfault, screen
    p = make(env)
    screen.mute(p.db, "checks", "env:check-host:box-a", 12, why="host flaps; recovery owned by its check")
    assert envfault.record(p, {"sig": "env:check-host:box-a", "kind": "host", "host": "box-a", "cmd": "x"})
    assert [r["status"] for r in p.db.q("SELECT status FROM events WHERE fingerprint LIKE 'env:check-host:box-a:%'")] \
        == ["muted"]
    assert [m["count"] for m in screen.mutes(p.db) if m["match"] == "env:check-host:box-a"] == [1]
    assert envfault.record(p, {"sig": "env:check-host:box-b", "kind": "host", "host": "box-b", "cmd": "x"})
    assert [r["status"] for r in p.db.q("SELECT status FROM events WHERE fingerprint LIKE 'env:check-host:box-b:%'")] \
        == ["queued"]
