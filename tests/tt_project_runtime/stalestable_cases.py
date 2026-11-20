# Stale stable copy tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. `ttp upgrade <name>` compares the
# stable copy (~/.tt-project/lib/current) against the newest release on the host before it merges.
from __future__ import annotations


def _fake_release(root, version):
    """A plugin folder `ttp setup` could install from, at `version`."""
    (root / "runtime" / "ttp").mkdir(parents=True)
    (root / "runtime" / "ttp" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (root / "template").mkdir()
    (root / "bin").mkdir()
    (root / "bin" / "ttp").write_text("#!/bin/sh\n")
    return root


def _stale_stable(env, version="0.0.1"):
    _install_template(env)
    init = env["home"] / "lib" / "current" / "runtime" / "ttp" / "__init__.py"
    init.write_text(f'__version__ = "{version}"\n')
    return env["home"] / "lib" / "current"


def _no_checkout_candidate(monkeypatch):
    from ttp import cli
    monkeypatch.setattr(cli, "PLUGIN_ROOT", cli.PLUGIN_ROOT / "no-such-plugin")   # as from a harness copy


def test_stale_stable_upgrade_refreshes_from_the_newer_setup_checkout_and_reruns(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, release
    monkeypatch.delenv("TTP_TEST_NO_REFRESH")
    _no_checkout_candidate(monkeypatch)
    cur = _stale_stable(env)
    src = _fake_release(tmp_path / "checkout", "0.0.5")
    (env["home"] / "lib" / release.SETUP_SOURCE).write_text(f"{src}\n")
    ran, execd = [], []

    def setup(cmd, timeout=None):       # the checkout's setup moves lib/current on to 0.0.5
        ran.append(cmd)
        (cur / "runtime" / "ttp" / "__init__.py").write_text('__version__ = "0.0.5"\n')
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(cli.subprocess, "run", setup)
    monkeypatch.setattr(cli.os, "execve", lambda exe, argv, e: execd.append((argv, e.get(cli.UPGRADE_REFRESHED))))
    monkeypatch.setattr(cli.sys, "argv", ["ttp", "upgrade", "demo"])
    cli._refresh_stable_copy("demo")
    assert ran == [[cli.sys.executable, str(src.resolve() / "bin" / "ttp"), "setup"]]
    assert execd == [([cli.sys.executable, str(cur / "bin" / "ttp"), "upgrade", "demo"], "1")]
    assert "older than 0.0.5" in capsys.readouterr().out


def test_stale_stable_upgrade_names_the_refresh_command_when_setup_fails(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    monkeypatch.delenv("TTP_TEST_NO_REFRESH")
    _no_checkout_candidate(monkeypatch)
    _stale_stable(env)
    cache = Path(os.environ["CLAUDE_CONFIG_DIR"])
    src = _fake_release(cache / "plugins" / "cache" / "tenstorrent-skills" / "tt-project" / "0.0.7", "0.0.7")
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, timeout=None: subprocess.CompletedProcess(cmd, 3))
    monkeypatch.setattr(cli.os, "execve", lambda *a: pytest.fail("no rerun after a failed refresh"))
    cli._refresh_stable_copy("demo")
    err = capsys.readouterr().err
    assert "is 0.0.1, older than 0.0.7" in err and "exited 3" in err
    assert f"`{src.resolve() / 'bin' / 'ttp'} setup`" in err and "rerun `ttp upgrade demo`" in err


def test_stale_stable_upgrade_rerun_only_warns_and_up_to_date_does_nothing(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, release
    monkeypatch.delenv("TTP_TEST_NO_REFRESH")
    _no_checkout_candidate(monkeypatch)
    _stale_stable(env)
    src = _fake_release(tmp_path / "checkout", "0.0.5")
    (env["home"] / "lib" / release.SETUP_SOURCE).write_text(f"{src}\n")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("no second setup"))
    monkeypatch.setenv(cli.UPGRADE_REFRESHED, "1")      # the rerun after a refresh never refreshes again
    cli._refresh_stable_copy("demo")
    assert "the refresh did not take" in capsys.readouterr().err
    monkeypatch.delenv(cli.UPGRADE_REFRESHED)
    _stale_stable(env, "0.0.5")                           # stable copy as new as the checkout: nothing to say
    cli._refresh_stable_copy("demo")
    assert capsys.readouterr() == ("", "")
    _stale_stable(env)
    (release.forced_mark()).write_text("0.0.1\n")         # a deliberate `ttp setup --force` downgrade stays
    cli._refresh_stable_copy("demo")
    assert capsys.readouterr() == ("", "")


def test_stale_stable_upgrade_auto_and_apply_never_refresh(env, monkeypatch):
    """Only an upgrade by hand refreshes: the daemon's --auto and a task's --apply merge what is installed."""
    import types
    from ttp import cli
    calls = []
    monkeypatch.setattr(cli, "_refresh_stable_copy", lambda name: calls.append(name))
    monkeypatch.setattr(cli, "remote_entry", lambda name: None)
    monkeypatch.setattr(cli, "need", lambda name, argv: types.SimpleNamespace(name=name))
    monkeypatch.setattr(cli, "cyclenotice_paths", lambda p: [])
    monkeypatch.setattr(cli, "_cycle_hold", lambda *a, **k: (_ for _ in ()).throw(SystemExit(0)))
    for extra in (["--auto"], ["--apply", "abc123"], []):
        with pytest.raises(SystemExit):
            cli.main(["upgrade", "demo", *extra])
    assert calls == ["demo"]


def test_stale_stable_setup_records_its_source_folder(env, tmp_path):
    from ttp import cli, release
    _install_template(env)
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["setup", "--bin-dir", str(tmp_path / "bin"), "--force"])
    rec = (env["home"] / "lib" / release.SETUP_SOURCE).read_text().strip()
    assert rec == str(cli.PLUGIN_ROOT.resolve())
    assert release.newest_source(claude_dir=tmp_path / "none") == (cli.__version__, cli.PLUGIN_ROOT.resolve())
