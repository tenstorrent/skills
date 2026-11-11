# Throwaway HOME for check runs (scratchhome) tests of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new scratchhome tests here, not there.
from __future__ import annotations

_PROBE = ('echo hi > ~/x && mkdir -p ~/.config && echo y > ~/.config/y && '
          'printf "%s\\n%s\\n%s\\n" "$HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" > {out} && '
          'ls -A "$HOME" "$HOME/.config" >> {out} && cat ~/.gitconfig >> {out}')


def _real_home(tmp_path, monkeypatch):
    """A stand-in for the account's HOME, with a .gitconfig naming an identity and a credential beside it."""
    home = tmp_path / "realhome"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = Scratch Tester\n\temail = scratch@example.com\n")
    (home / ".git-credentials").write_text("https://u:secret@example.com\n")
    monkeypatch.setenv("HOME", str(home))
    from ttp import scratchhome
    monkeypatch.setattr(scratchhome, "_own", [])   # a foreground run's own folder, removed when it exits
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "GIT_CONFIG_GLOBAL",
                "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)
    return home


def _home_marks(home):
    return sorted(str(p.relative_to(home)) for p in home.rglob("*"))


def test_a_ttp_checks_run_writes_its_own_scratch_home_never_the_real_one(env, tmp_path, monkeypatch):
    # (a) ~/x and ~/.config/y land in the run's throwaway HOME, which is gone after it; the real one is unchanged.
    from ttp import cli, scratchhome
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    home = _real_home(tmp_path, monkeypatch)
    before = _home_marks(home)
    out = tmp_path / "out"
    cli.main(["checks", "--", "sh", "-c", _PROBE.format(out=out)])
    seen = out.read_text().splitlines()
    scratch, config, cache = seen[:3]
    assert scratch != str(home) and not scratch.startswith(str(home) + "/")
    assert config == f"{scratch}/.config" and not cache.startswith(scratch)
    assert "x" in seen and "y" in seen and "\tname = Scratch Tester" in seen   # the copied .gitconfig
    assert ".git-credentials" not in seen   # never copied
    assert _home_marks(home) == before and scratch == f"{scratchhome.own_folder()}/home"


def test_git_commit_in_a_check_uses_the_copied_gitconfig(env, tmp_path, monkeypatch):
    # (b) a check's commit gets the identity of the account's .gitconfig, copied into the scratch HOME.
    from ttp import cli, scratchhome
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    home = _real_home(tmp_path, monkeypatch)
    out = tmp_path / "who"
    sub = tmp_path / "sub"
    cli.main(["checks", "--", "sh", "-c", f"git init -q {sub} && git -C {sub} commit -q --allow-empty -m x && "
              f"git -C {sub} log -1 --format=%an/%ae > {out} && printf %s \"$HOME\" >> {out}"])
    assert out.read_text().splitlines() == ["Scratch Tester/scratch@example.com", f"{scratchhome.own_folder()}/home"]
    assert str(home) not in out.read_text()


def test_checks_scratch_home_false_runs_a_check_with_the_real_home(env, tmp_path, monkeypatch):
    from ttp import cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    home = _real_home(tmp_path, monkeypatch)
    p.set_config("checks.scratch_home", False)
    out = tmp_path / "out"
    cli.main(["checks", "--", "sh", "-c", f'printf "%s" "$HOME" > {out}'])
    assert out.read_text() == str(home)


def test_ttp_land_honours_checks_scratch_home_and_home_files(env, tmp_path, monkeypatch):
    """`ttp land` reads the project's checks settings itself: no `ttp push` or `ttp checks` in its
    process sets push.check_cfg, so a default would ignore the project's opt-out."""
    from ttp import push
    out = tmp_path / "out"
    land, p, repo, t, head = _land_setup(env, monkeypatch, [f'printf "%s\\n" "$HOME" >> {out}; ls -A "$HOME" >> {out}'])
    home = _real_home(tmp_path, monkeypatch)
    (home / ".extra").write_text("x\n")
    monkeypatch.setattr(push, "check_cfg", None)
    p.set_config("checks.scratch_home", False)
    said = []
    assert land.land(p, repo, head, t, said.append) == 0, said
    assert out.read_text().splitlines()[0] == str(home), said
    out.unlink()
    p.set_config("checks.scratch_home", True)
    p.set_config("checks.home_files", [".extra"])
    _git_out(repo, "checkout", "-q", "work")
    _commit(repo, "b.txt", "b\n")
    assert land.land(p, repo, _git_out(repo, "rev-parse", "HEAD"), t, said.append) == 0, said
    lines = out.read_text().splitlines()
    assert lines[0] != str(home) and lines[0].endswith("/home"), lines
    assert ".extra" in lines and ".gitconfig" not in lines, lines


def test_every_check_env_caller_passes_the_project_config_or_runs_where_check_cfg_is_set():
    """Only `ttp push` (push._run) and `ttp checks` (cli.cmd_checks) set push.check_cfg for their
    process; a check_env call in any other module names cfg, or it gets the default scratch HOME."""
    import re
    from pathlib import Path
    from ttp import push
    for f in Path(push.__file__).parent.glob("*.py"):
        if f.name in ("push.py", "cli.py"):
            continue
        for call in re.findall(r"push\.check_env\((.*)", f.read_text()):
            assert "cfg=" in call, f"{f.name}: push.check_env({call}"


def test_check_env_inside_a_check_never_builds_its_home_in_the_runs_scratch_home(tmp_path, monkeypatch):
    # A harness test under a guarded `ttp checks` inherits the run's guard, output dir and FILES_ENV: a
    # check_env it calls gets a HOME of its own, never the run's scratch HOME the test session runs under,
    # whose .gitconfig stays as it was.
    from ttp import checkdirs, push, scratchhome
    home = _real_home(tmp_path, monkeypatch)
    output = tmp_path / "check-reports" / "r1"
    output.mkdir(parents=True)
    (tmp_path / "run").mkdir()
    outer = tmp_path / checkdirs.FOLDER / "r1" / checkdirs.SCRATCH / scratchhome.FOLDER
    outer.mkdir(parents=True)
    (outer / ".gitconfig").write_text("[user]\n\tname = Outer Run\n")
    monkeypatch.setenv("TTP_CHECKS_TMP_FD", "9")
    monkeypatch.setenv("TTP_CHECK_OUTPUT_DIR", str(output))
    monkeypatch.setenv(scratchhome.FILES_ENV, '[".gitconfig"]')
    got = push.check_env("checks", run_dir=str(tmp_path / "run"), cfg={})
    assert got["HOME"] == f"{scratchhome.own_folder()}/home" and got["HOME"] != str(outer)
    assert (outer / ".gitconfig").read_text() == "[user]\n\tname = Outer Run\n"
    assert Path(got["HOME"], ".gitconfig").read_text() == (home / ".gitconfig").read_text()
    monkeypatch.delenv(scratchhome.FILES_ENV)   # the run's own process (`ttp checks`): the managed scratch
    assert push.check_env("checks", run_dir=str(tmp_path / "run"), cfg={})["HOME"] == str(outer)


def test_checks_home_files_refuses_credentials_ssh_gh_and_paths_outside_home(tmp_path, monkeypatch):
    # (c) a credential store (also by a symlink or a folder holding one), ~/.ssh, gh's config and a path
    # leaving HOME are refused by the setting and never copied by apply, given one anyway.
    from ttp import coordinator, scratchhome
    home = _real_home(tmp_path, monkeypatch)
    (home / ".ssh").mkdir()
    (home / ".ssh" / "config").write_text("Host x\n")
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "config.yml").write_text("editor: vi\n")
    (home / "innocent").symlink_to(home / ".git-credentials")
    for bad in (".git-credentials", ".netrc", ".ssh/config", ".ssh", ".config/gh/config.yml", "innocent",
                ".", "../etc/passwd", "/etc/passwd", "~/.gitconfig", ""):
        with pytest.raises(ValueError):
            scratchhome.settings({"checks": {"home_files": [bad]}}, home)
        with pytest.raises(ValueError):
            coordinator.USER_SETTABLE["checks.home_files"]([bad])   # `ttp config` refuses it too
    assert scratchhome.settings({}) == (True, [".gitconfig"])
    assert scratchhome.settings({"checks": {"scratch_home": "off", "home_files": ".gitconfig .config/git/config"}}, home) \
        == (False, [".gitconfig", ".config/git/config"])
    got = scratchhome.apply({"HOME": str(home)}, tmp_path / "run",
                            [".git-credentials", "innocent", ".ssh/config", ".config/gh/config.yml", ".gitconfig"])
    assert _home_marks(Path(got["HOME"])) == sorted([".config", ".gitconfig", ".local", ".local/share", ".local/state"])


def test_a_check_in_the_scratch_home_reuses_the_real_tool_cache(env, tmp_path, monkeypatch):
    # $HOME/.cache in a check's scratch HOME is the cache in force (the real HOME's .cache, or
    # XDG_CACHE_HOME where set), so pip, uv or plugin caches are not rebuilt each run; a cache holding a
    # credential store is never linked; removing the run's folder leaves the cache; scratch_home false: real HOME.
    from ttp import checkdirs, cli, scratchhome
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    home = _real_home(tmp_path, monkeypatch)
    secret = next(home.glob(".git-cred*"))
    (home / ".cache" / "pip").mkdir(parents=True)
    (home / ".cache" / "pip" / "wheel").write_text("warm\n")
    out = tmp_path / "out"
    probe = (f'printf "%s\\n%s\\n" "$HOME" "$XDG_CACHE_HOME" > {out} && cat "$HOME"/.cache/pip/wheel >> {out} && '
             f'echo new > "$HOME"/.cache/pip/more && readlink -f "$HOME"/.cache >> {out}')
    cli.main(["checks", "--", "sh", "-c", probe])
    scratch, cache, warm, real = out.read_text().splitlines()
    assert scratch == f"{scratchhome.own_folder()}/home" and cache == str(home / ".cache") and warm == "warm"
    assert real == os.path.realpath(home / ".cache") and (home / ".cache" / "pip" / "more").read_text() == "new\n"
    # XDG_CACHE_HOME set: that cache is linked in, the real HOME's is left alone
    xdg = tmp_path / "xdgcache"
    xdg.mkdir()
    got = scratchhome.apply({"HOME": str(home), "XDG_CACHE_HOME": str(xdg)}, tmp_path / "run1", [])
    assert os.path.realpath(Path(got["HOME"]) / ".cache") == os.path.realpath(xdg) and got["XDG_CACHE_HOME"] == str(xdg)
    # a cache that holds or is a credential store is refused: nothing linked, the shared checks cache instead
    for i, bad in enumerate((home, secret, "relative/cache")):   # holds one (also by a link), is one, not absolute
        got = scratchhome.apply({"HOME": str(home), "XDG_CACHE_HOME": str(bad)}, tmp_path / f"run{i}", [".gitconfig"])
        assert not (Path(got["HOME"]) / ".cache").is_symlink() and got["XDG_CACHE_HOME"] == str(bad)
    (tmp_path / "homelink").symlink_to(home)
    (home / ".cache").rename(tmp_path / "cache-aside")
    (home / ".cache").symlink_to(tmp_path / "homelink")
    got = scratchhome.apply({"HOME": str(home)}, tmp_path / "run9", [])
    assert not (Path(got["HOME"]) / ".cache").is_symlink() and got["XDG_CACHE_HOME"] == str(scratchhome.cache_dir())
    (home / ".cache").unlink()
    (tmp_path / "cache-aside").rename(home / ".cache")
    # removing a run's folder unlinks the link and never empties the cache it points to
    run4 = tmp_path / "run4"
    got = scratchhome.apply({"HOME": str(home)}, run4, [])
    assert (Path(got["HOME"]) / ".cache").is_symlink()
    parent = os.open(tmp_path, os.O_RDONLY)
    try:
        checkdirs._remove(parent, "run4")
    finally:
        os.close(parent)
    assert not run4.exists() and (home / ".cache" / "pip" / "wheel").read_text() == "warm\n"
    p.set_config("checks.scratch_home", False)
    cli.main(["checks", "--", "sh", "-c", f'printf "%s" "$HOME" > {out}'])
    assert out.read_text() == str(home)


def test_a_spilled_check_runs_in_a_scratch_home_in_its_run_folder_there(tmp_path):
    # (d) on the other host: HOME in the run's folder there (removed with it), that host's own
    # .gitconfig copied in, nothing shipped from here, its real HOME unchanged; without the setting, as before.
    from ttp import scratchhome
    repo = _rc_repo(tmp_path)
    rhome = tmp_path / "rhome"
    rhome.mkdir()
    (rhome / ".gitconfig").write_text("[user]\n\tname = There\n")
    (rhome / ".netrc").write_text("machine x password y\n")
    before = _home_marks(rhome)
    out = tmp_path / "seen"
    files = json.dumps([".gitconfig", ".netrc"])   # a refused entry that reached the env is still dropped
    rc, said = _rc_run(tmp_path, repo, _PROBE.format(out=out), HOME=str(rhome), XDG_CACHE_HOME="", TTP_HOME="", **{scratchhome.FILES_ENV: files})
    assert rc == 0, said
    seen = out.read_text().splitlines()
    assert "/ttp-check." in seen[0] and "/.git/ttp-env/home." in seen[0] and seen[1] == f"{seen[0]}/.config"
    assert "x" in seen and "y" in seen and "\tname = There" in seen and ".netrc" not in seen
    assert seen[2].endswith("/host-home/checks-cache") and not seen[2].startswith(seen[0])   # the shared cache stays with the host's tt-project folder
    assert _home_marks(rhome) == before and _rc_left(tmp_path) == []
    # that host's own .cache is linked in as the scratch HOME's (scratchhome.cache_link), and outlives the run
    (rhome / ".cache").mkdir()
    (rhome / ".cache" / "w").write_text("warm\n")
    rc, said = _rc_run(tmp_path, repo, f'printf "%s\\n" "$XDG_CACHE_HOME" > {out} && cat "$HOME"/.cache/w >> {out} && '
                       f'readlink "$HOME"/.cache >> {out}', HOME=str(rhome), XDG_CACHE_HOME="", TTP_HOME="",
                       **{scratchhome.FILES_ENV: files})
    assert rc == 0, said
    assert out.read_text().splitlines() == [str(rhome / ".cache"), "warm", str(rhome / ".cache")]
    assert (rhome / ".cache" / "w").exists() and _rc_left(tmp_path) == []
    rc, said = _rc_run(tmp_path, repo, f'printf "%s" "$HOME" > {out}', HOME=str(rhome))
    assert rc == 0 and out.read_text() == str(rhome), said
