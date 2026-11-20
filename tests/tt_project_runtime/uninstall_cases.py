# `ttp uninstall` tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new uninstall tests here, not there.
from __future__ import annotations

_UNINSTALL_STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
if [ "$(basename "$0")" = crontab ]; then
  case "$1" in
    -l) [ -f "$STUB_TAB" ] && cat "$STUB_TAB" || { echo "no crontab" >&2; exit 1; } ;;
    -) cat > "$STUB_TAB" ;;
  esac
fi
exit 0
"""


def _uninstall_box(env, tmp_path):
    """A temp HOME with everything `ttp setup`, `ttp start`, the notifier and a kept tunnel install, plus the
    user's own unit, agent and crontab lines. systemctl, launchctl, crontab and loginctl are stubs."""
    from ttp import cli, service, shortcut
    from ttp.project import Project, register
    home = tmp_path / "userhome"
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("systemctl", "launchctl", "crontab", "loginctl"):
        (stubs / name).write_text(_UNINSTALL_STUB)
        (stubs / name).chmod(0o755)
    units = home / ".config" / "systemd" / "user"
    agents = home / "Library" / "LaunchAgents"
    units.mkdir(parents=True)
    agents.mkdir(parents=True)
    for f in ("tt-project-demo.service", "tt-project-demo-watchdog.service", "tt-project-demo-watchdog.timer",
              "tt-project-notifier.service", "com.tt-project.tunnel.host-a.service", "backup.service"):
        (units / f).write_text("[Unit]\n")
    for f in ("com.tt-project.tunnel.host-a.plist", "com.example.backup.plist"):
        (agents / f).write_text("<plist/>\n")
    import plistlib
    for app, bid in (("tt-project.app", "com.tt-project.notifier"), ("Other.app", "com.example.other")):
        (home / "Applications" / app / "Contents").mkdir(parents=True)
        (home / "Applications" / app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bid}))
    root = tmp_path / "proj"
    (root / "tt-project" / "state").mkdir(parents=True)
    (root / "tt-project" / "state" / "project.db").write_text("keep me\n")
    (root / "notes.txt").write_text("the user's own file\n")
    register("demo", {"host": "testhost", "dir": str(root)})
    lib = env["home"] / "lib"
    (lib / "9.9.9" / "bin").mkdir(parents=True)
    (lib / "current").symlink_to(lib / "9.9.9")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ttp").write_text(cli.shim_text(sys.executable, lib / "current" / "bin" / "ttp"))
    sc = shortcut.path()
    sc.parent.mkdir(parents=True)
    sc.write_text(shortcut.TEXT)
    mine = "0 1 * * * /usr/local/bin/backup.sh  # tt-project:/not/ours\n"
    decoy = "5 * * * * echo hi # tt-project:/elsewhere/tt-project\n"   # the tag alone does not make it ttp's
    tab = tmp_path / "crontab.txt"
    tab.write_text(mine + "\n".join(service._cron_lines(Project(root))) + "\n" + decoy)
    (tmp_path / "cache" / "ttp").mkdir(parents=True, exist_ok=True)   # XDG_CACHE_HOME (the env fixture)
    daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "fake-ttp-daemon"])
    (root / "tt-project" / "state" / "daemon.pid").write_text(f"{daemon.pid}\n")
    run_env = {**os.environ, "HOME": str(home), "PATH": f"{stubs}{os.pathsep}/usr/bin{os.pathsep}/bin",
               "STUB_LOG": str(tmp_path / "calls.log"), "STUB_TAB": str(tab), "PYTHONPATH": str(RUNTIME),
               "ZDOTDIR": str(home), "XDG_CONFIG_HOME": str(home / ".config")}    # never the real ones

    def ttp(*args, stdin="", extra_env=None):
        r = subprocess.run([sys.executable, "-m", "ttp", "uninstall", "--bin-dir", str(bindir), *args],
                           env={**run_env, **(extra_env or {})}, input=stdin, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout.replace(str(tmp_path), "<tmp>"), r.stderr
    return {"ttp": ttp, "home": home, "units": units, "agents": agents, "root": root, "lib": lib, "bin": bindir,
            "shortcut": sc, "tab": tab, "mine": mine, "decoy": decoy, "daemon": daemon, "log": tmp_path / "calls.log"}


def _uninstall_tree(top):
    return {str(f): (f.read_bytes() if f.is_file() else b"") for f in sorted(top.rglob("*"))
            if f.name != "calls.log" and "__pycache__" not in f.parts}


def _uninstall_calls(log):
    calls = log.read_text().splitlines() if log.exists() else []
    return [c for c in calls if c not in ("crontab -l",)]


def test_uninstall_dry_run_and_a_declined_question_change_nothing(env, tmp_path):
    b = _uninstall_box(env, tmp_path)
    try:
        before = _uninstall_tree(tmp_path)
        rc, out, err = b["ttp"]("--dry-run")
        assert rc == 0, err
        for want in ("would remove:", "stop and remove systemd user unit tt-project-demo-watchdog.timer",
                     "stop and remove systemd user unit tt-project-notifier.service",
                     "stop and remove launchd agent com.tt-project.tunnel.host-a",
                     "remove 2 crontab line(s) ttp added", "@reboot ... # tt-project:<tmp>/proj/tt-project",
                     f"end the daemon of demo (pid {b['daemon'].pid})", "remove the /tt-project shortcut",
                     "remove the ttp command <tmp>/bin/ttp", "remove the installed lib",
                     "left in place", "rm -rf <tmp>/proj/tt-project", "loginctl disable-linger",
                     "claude plugin marketplace remove tenstorrent-skills"):
            assert want in out, (want, out)
        assert "backup" not in out and "/elsewhere/" not in out
        # The watchdog timer is stopped before the service it starts.
        assert out.index("tt-project-demo-watchdog.timer") < out.index("tt-project-demo-watchdog.service")
        rc, out2, err = b["ttp"](stdin="n\n")     # asked and declined
        assert rc == 1 and "nothing changed" in err
        assert _uninstall_tree(tmp_path) == before and not _uninstall_calls(b["log"])
        assert b["daemon"].poll() is None
    finally:
        b["daemon"].kill()
        b["daemon"].wait()


def test_uninstall_removes_only_ttps_own_entries_and_a_second_run_is_harmless(env, tmp_path):
    import json
    b = _uninstall_box(env, tmp_path)
    try:
        rc, out, err = b["ttp"](stdin="y\n")
        assert rc == 0, err
        assert "done" in out and "not done" not in err
        assert b["daemon"].wait(timeout=30) is not None
        assert sorted(f.name for f in b["units"].iterdir()) == ["backup.service"]
        assert sorted(f.name for f in b["agents"].iterdir()) == ["com.example.backup.plist"]
        assert b["tab"].read_text() == b["mine"] + b["decoy"]
        assert not b["lib"].exists() and not (b["bin"] / "ttp").exists() and not b["shortcut"].exists()
        assert sorted(f.name for f in (b["home"] / "Applications").iterdir()) == ["Other.app"], "the notifier app goes"
        assert (b["root"] / "tt-project" / "state" / "project.db").read_text() == "keep me\n"
        assert (b["root"] / "notes.txt").exists()
        assert json.loads((env["home"] / "registry.json").read_text())["projects"]["demo"]
        calls = _uninstall_calls(b["log"])
        assert "systemctl --user disable --now tt-project-demo.service" in calls
        assert f"launchctl bootout gui/{os.getuid()}/com.tt-project.tunnel.host-a" in calls
        assert not [c for c in calls if "backup" in c or c.startswith("loginctl")]
        for left in ("rm -rf <tmp>/proj/tt-project", "loginctl disable-linger", "ttp's cache: rm -rf <tmp>/cache/ttp",
                     "per-user files (project list, saved keys, settings): rm -rf <tmp>/home"):
            assert left in out, (left, out)
        b["log"].unlink()
        before = _uninstall_tree(tmp_path)
        rc, out, err = b["ttp"]("--yes")
        assert rc == 0, err
        assert "remove nothing" in out and "left in place" in out
        assert _uninstall_tree(tmp_path) == before and not _uninstall_calls(b["log"])
    finally:
        b["daemon"].kill()
        b["daemon"].wait()


def test_uninstall_is_refused_inside_a_run(env, monkeypatch, capsys):
    from ttp import cli
    monkeypatch.setenv("TTP_RUN_DIR", str(env["tmp"] / "run"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env["tmp"] / "config"))   # never the real one
    with pytest.raises(SystemExit) as e:
        cli.main(["uninstall", "--dry-run"])
    assert e.value.code == 2 and "refused inside a run" in capsys.readouterr().err


def test_uninstall_removes_only_the_installers_marked_path_line(env, tmp_path):
    b = _uninstall_box(env, tmp_path)
    home, mark = b["home"], " # added by tt-project installer"
    bashrc, zshrc, fish = home / ".bashrc", home / ".zshrc", home / ".config" / "fish" / "conf.d" / "tt-project.fish"
    own = b"alias ll='ls -l'\r\n# my PATH\nexport PATH=\"$HOME/bin:$PATH\"\n"
    bashrc.write_bytes(b"# top\n" + f'export PATH="$HOME/.local/bin:$PATH"{mark}\n'.encode() + own)
    zshrc.write_bytes(own)                       # no marked line: left alone
    login = home / ".bash_login"                 # macOS bash's login profile when it is the first found
    login.write_bytes(own + f'export PATH="$HOME/.local/bin:$PATH"{mark}'.encode())
    fish.parent.mkdir(parents=True)
    fish.write_text(f"fish_add_path -g $HOME/.local/bin{mark}\n")
    dotfiles = tmp_path / "dotfiles" / "profile"   # a dotfile manager's symlink stays a symlink
    dotfiles.parent.mkdir()
    dotfiles.write_bytes(own + f'export PATH="$HOME/.local/bin:$PATH"{mark}\n'.encode())
    (home / ".profile").symlink_to(dotfiles)
    try:
        rc, out, err = b["ttp"]("--dry-run")
        assert rc == 0, err
        assert "remove the PATH line the installer added to <tmp>/userhome/.bashrc" in out
        assert "added to <tmp>/userhome/.config/fish/conf.d/tt-project.fish" in out and ".zshrc" not in out
        assert "added to <tmp>/userhome/.bash_login" in out
        rc, out, err = b["ttp"]("--yes")
        assert rc == 0 and "not done" not in err, err
        assert bashrc.read_bytes() == b"# top\n" + own and zshrc.read_bytes() == own
        assert login.read_bytes() == own
        assert (home / ".profile").is_symlink() and dotfiles.read_bytes() == own
        assert not fish.exists() and fish.parent.is_dir()
        rc, out, err = b["ttp"]("--yes")
        assert rc == 0 and "remove nothing" in out and "PATH line the installer" not in out
    finally:
        b["daemon"].kill()
        b["daemon"].wait()


def test_uninstall_removes_the_fish_path_file_under_xdg_config_home(env, tmp_path):
    b = _uninstall_box(env, tmp_path)
    xdg = tmp_path / "xdg"
    fish = xdg / "fish" / "conf.d" / "tt-project.fish"
    fish.parent.mkdir(parents=True)
    fish.write_text("fish_add_path -g $HOME/.local/bin # added by tt-project installer\n")
    try:
        rc, out, err = b["ttp"]("--yes", extra_env={"XDG_CONFIG_HOME": str(xdg)})
        assert rc == 0 and "not done" not in err, err
        assert not fish.exists()
    finally:
        b["daemon"].kill()
        b["daemon"].wait()


def test_uninstall_tests_never_touch_the_real_xdg_config_home(env, tmp_path, monkeypatch):
    # The developer's own XDG_CONFIG_HOME stays out of reach: the box pins it inside tmp_path.
    real = tmp_path / "real-config" / "fish" / "conf.d" / "tt-project.fish"
    real.parent.mkdir(parents=True)
    real.write_text("fish_add_path -g $HOME/.local/bin # added by tt-project installer\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "real-config"))
    b = _uninstall_box(env, tmp_path)
    pinned = b["home"] / ".config" / "fish" / "conf.d" / "tt-project.fish"
    pinned.parent.mkdir(parents=True)
    pinned.write_text(real.read_text())
    try:
        rc, out, err = b["ttp"]("--yes")
        assert rc == 0 and "not done" not in err, err
        assert real.exists() and not pinned.exists()
    finally:
        b["daemon"].kill()
        b["daemon"].wait()
