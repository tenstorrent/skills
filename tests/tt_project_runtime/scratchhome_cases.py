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
    assert seen[2].startswith(str(rhome))   # the shared cache stays on the host's real HOME
    assert _home_marks(rhome) == before and _rc_left(tmp_path) == []
    rc, said = _rc_run(tmp_path, repo, f'printf "%s" "$HOME" > {out}', HOME=str(rhome))
    assert rc == 0 and out.read_text() == str(rhome), said
