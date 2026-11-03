# checks.fallback_lane tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. Add new tests of the lane a paused checks.wrap_cmd falls back to here.
from __future__ import annotations


def _lock_script(tmp_path, name: str):
    """A stand-in for `ttp lock <name> --`: while <name>.paused exists it refuses as `ttp lock` does
    ("held by pause on <name>", exit 75) and runs nothing; else it logs its name and runs the command."""
    log, pause = tmp_path / "wrap.log", tmp_path / f"{name}.paused"
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/bin/sh\nif [ -e {shlex.quote(str(pause))} ]; then\n"
                      f"  echo 'ttp lock: held by pause on {name}: {name} is paused (maintenance)' >&2; exit 75\nfi\n"
                      f"echo {name} >> {shlex.quote(str(log))}\nexec \"$@\"\n")
    script.chmod(0o755)
    return script, pause, log


def _ran(log) -> list[str]:
    return log.read_text().split() if log.exists() else []


def test_checks_fallback_lane_runs_paused_wrapped_commands_there_and_says_so(env, tmp_path, monkeypatch, capsys):
    # checks.wrap_cmd's lock paused, checks.fallback_lane a wrapper: the refused command and every later
    # wrapped one run behind the fallback, never the paused wrapper; the summary and checks.json say so.
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    dev, dev_pause, log = _lock_script(tmp_path, "dev")
    cpu, _, _ = _lock_script(tmp_path, "cpu")
    a, b = tmp_path / "a", tmp_path / "b"
    p.set_config("checks.wrap_cmd", str(dev))
    p.set_config("checks.fallback_lane", str(cpu))
    dev_pause.touch()
    capsys.readouterr()
    cli.main(["checks", "--fresh", "--", f"touch {a}", f"touch {b}"])
    out = capsys.readouterr().out
    rec = json.loads((run / "checks.json").read_text())
    assert a.exists() and b.exists() and _ran(log) == ["cpu", "cpu"], _ran(log)
    assert rec["passed"] is True and rec["fallback_lane"]["lane"] == str(cpu)
    assert rec["fallback_lane"]["paused"] == "dev" and rec["fallback_lane"]["commands"] == [f"touch {a}", f"touch {b}"]
    assert "2 ran on checks.fallback_lane" in out and "NOT behind checks.wrap_cmd" in out and "dev is paused" in out
    assert "held by pause on dev" in (run / "checks.log").read_text()
    # The pause starting mid-run: what ran behind the wrapper before it stays recorded there.
    a.unlink()
    b.unlink()
    log.unlink()
    dev_pause.unlink()
    cli.main(["checks", "--fresh", "--", f"touch {dev_pause}", f"touch {b}"])
    rec = json.loads((run / "checks.json").read_text())
    assert b.exists() and _ran(log) == ["dev", "cpu"] and rec["fallback_lane"]["commands"] == [f"touch {b}"]
    assert "1 ran on checks.fallback_lane" in capsys.readouterr().out
    # "unwrapped": no wrapper at all, with TTP_NO_DEVICE=1 set, a CPU-only run.
    log.unlink()
    p.set_config("checks.fallback_lane", "unwrapped")
    cli.main(["checks", "--fresh", "--", f"test \"$TTP_NO_DEVICE\" = 1 && touch {a}"])
    rec = json.loads((run / "checks.json").read_text())
    assert a.exists() and not log.exists() and rec["passed"] is True and rec["fallback_lane"]["lane"] == "unwrapped"
    assert "ran on checks.fallback_lane 'unwrapped'" in capsys.readouterr().out


def test_checks_fallback_lane_off_waits_for_the_pause_unchanged(env, tmp_path, monkeypatch, capsys):
    # No checks.fallback_lane (the default): a paused wrapper's command waits, named as the pause, as before.
    from ttp import cli, project, push
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    dev, dev_pause, log = _lock_script(tmp_path, "dev")
    a = tmp_path / "a"
    p.set_config("checks.wrap_cmd", str(dev))
    dev_pause.touch()
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh", "--", f"touch {a}"])
    err = capsys.readouterr().err
    rec = json.loads((run / "checks.json").read_text())
    assert e.value.code == 75 and not a.exists() and not log.exists()
    assert "held by pause on dev" in err and "ttp lock --probe dev" in err and "checks.fallback_lane" not in err, err
    assert rec["passed"] is False and rec["held_by_pause"] == "dev" and "fallback_lane" not in rec
    assert push.fallback_lane_of({}) == ("", (), "") and push.fallback_lane_of({"checks": {"fallback_lane": " "}})[0] == ""
    assert not [x for x in project.config_problems({"checks": {"fallback_lane": "unwrapped"}}) if "fallback" in x]
    assert [x for x in project.config_problems({"checks": {"fallback_lane": 5}}) if "fallback_lane" in x]
    assert [x for x in project.config_problems({"checks": {"fallback_lane": "'open"}}) if "does not parse" in x]


def test_checks_fallback_lane_paused_or_missing_waits_and_never_fails(env, tmp_path, monkeypatch, capsys):
    # The fallback's own lock paused too: the run waits on both (exit 75, a probe of either), runs nothing
    # and passes nothing. One that cannot serve (missing, the paused wrapper itself, not a string) waits too.
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    dev, dev_pause, log = _lock_script(tmp_path, "dev")
    cpu, cpu_pause, _ = _lock_script(tmp_path, "cpu")
    a = tmp_path / "a"
    p.set_config("checks.wrap_cmd", str(dev))
    p.set_config("checks.fallback_lane", str(cpu))
    dev_pause.touch()
    cpu_pause.touch()
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh", "--", f"touch {a}"])
    err = capsys.readouterr().err
    rec = json.loads((run / "checks.json").read_text())
    assert e.value.code == 75 and not a.exists() and not log.exists()
    assert "held by pause on dev" in err and "is paused too (held by pause on cpu)" in err
    assert "ttp lock --probe 'dev|cpu'" in err, err
    assert rec["passed"] is False and rec["held_by_pause"] == "dev" and rec["fallback_lane"]["held_by_pause"] == "cpu"
    for bad, why in ((str(tmp_path / "missing.sh"), "missing or not executable"), (str(dev), "itself"),
                     (7, "must be a wrapper command")):
        p.set_config("checks.fallback_lane", bad)
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--fresh", "--", f"touch {a}"])
        err = capsys.readouterr().err
        rec = json.loads((run / "checks.json").read_text())
        assert e.value.code == 75 and "cannot serve" in err and why in err and "ttp lock --probe dev" in err, (bad, err)
        assert not a.exists() and rec["passed"] is False and rec["fallback_lane"]["problem"]


def test_checks_fallback_lane_unused_once_the_pause_lifts(env, tmp_path, monkeypatch, capsys):
    # A run that waited on the pause (fallback paused too) runs on checks.wrap_cmd itself once the pause
    # lifts: the fallback is only for a paused wrapper, and the record names no fallback then.
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    dev, dev_pause, log = _lock_script(tmp_path, "dev")
    cpu, cpu_pause, _ = _lock_script(tmp_path, "cpu")
    a = tmp_path / "a"
    p.set_config("checks.wrap_cmd", str(dev))
    p.set_config("checks.fallback_lane", str(cpu))
    dev_pause.touch()
    cpu_pause.touch()
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--fresh", "--", f"touch {a}"])
    assert e.value.code == 75 and not a.exists()
    dev_pause.unlink()   # lifted while the task waited on the probe
    capsys.readouterr()
    cli.main(["checks", "--fresh", "--", f"touch {a}"])
    out = capsys.readouterr().out
    rec = json.loads((run / "checks.json").read_text())
    assert a.exists() and _ran(log) == ["dev"] and rec["passed"] is True and "fallback_lane" not in rec
    assert "checks.fallback_lane" not in out
    # Only the fallback's pause lifting: the wrapper still paused, the run goes to the fallback.
    a.unlink()
    log.unlink()
    dev_pause.touch()
    cpu_pause.unlink()
    cli.main(["checks", "--fresh", "--", f"touch {a}"])
    rec = json.loads((run / "checks.json").read_text())
    assert a.exists() and _ran(log) == ["cpu"] and rec["fallback_lane"]["paused"] == "dev"
