# `ttp setup`'s and `ttp doctor`'s Codex plugin cache step (codexmarket.py) tests of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations

# A `codex` stand-in that logs its calls to $CX_LOG, fails a call containing $CX_FAIL (exit 3) and
# otherwise does what codex-cli 0.160 was measured to do: a marketplace is registered by its resolved
# folder, `marketplace add` refuses a second source, `remove` keeps the plugin's entry, `marketplace
# upgrade` copies a git source into $CODEX_HOME/.tmp/marketplaces/<name>, and `plugin add` replaces the
# cached copy, other version folders included, with the version the registered folder or snapshot serves.
CX_STUB = r'''
import json, os, pathlib, re, sys, time
a = sys.argv[1:]
home = pathlib.Path(os.environ["CODEX_HOME"])
with open(os.environ["CX_LOG"], "a") as f:
    f.write(" ".join(a) + "\n")
if os.environ.get("CX_HANG"):
    time.sleep(30)
if os.environ.get("CX_FAIL") and os.environ["CX_FAIL"] in " ".join(a):
    sys.exit(print("no network", file=sys.stderr) or 3)
cfg = home / "config.toml"
text = cfg.read_text() if cfg.exists() else ""
sect = re.compile(r"\[marketplaces\.tenstorrent-skills\]\n(?:[^\[\n].*\n?)*")
if a[:3] == ["plugin", "marketplace", "remove"]:
    cfg.write_text(sect.sub("", text))
elif a[:3] == ["plugin", "marketplace", "add"]:
    if sect.search(text):
        sys.exit(print("already added from a different source", file=sys.stderr) or 1)
    cfg.write_text(text + f'\n[marketplaces.tenstorrent-skills]\nsource_type = "local"\nsource = "{os.path.realpath(a[3])}"\n')
elif a[:3] == ["plugin", "marketplace", "upgrade"]:
    snap = home / ".tmp/marketplaces/tenstorrent-skills"
    if 'source_type = "git"' in sect.search(text).group(0):
        import shutil
        shutil.rmtree(snap, ignore_errors=True)
        shutil.copytree(re.search(r'source = "([^"]*)"', sect.search(text).group(0)).group(1), snap)
elif a[:2] == ["plugin", "add"]:
    src = pathlib.Path(re.search(r'source = "([^"]*)"', sect.search(text).group(0)).group(1))
    if 'source_type = "git"' in sect.search(text).group(0):
        src = home / ".tmp/marketplaces/tenstorrent-skills"
    rel = [p for p in json.loads((src / ".agents/plugins/marketplace.json").read_text())["plugins"]
           if p["name"] == "tt-project"][0]["source"]["path"]
    man = json.loads((src / rel / ".codex-plugin/plugin.json").read_text())
    cache = home / "plugins/cache/tenstorrent-skills/tt-project"
    for old in cache.glob("*"):
        old.rename(home / f"gone-{old.name}")
    (cache / man["version"] / ".codex-plugin").mkdir(parents=True)
    (cache / man["version"] / ".codex-plugin/plugin.json").write_text(json.dumps(man))
'''


def _cx_market(root, version, others=()):
    """A local Codex marketplace serving tt-project `version` (beside `others`), as the installer's
    clone of the skills repository is."""
    plugins = [{"name": n, "source": {"source": "local", "path": f"./plugins/{n}"}} for n in ("tt-project", *others)]
    (root / ".agents" / "plugins").mkdir(parents=True)
    (root / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps({"name": "tenstorrent-skills",
                                                                                "plugins": plugins}))
    (root / "plugins" / "tt-project" / ".codex-plugin").mkdir(parents=True)
    (root / "plugins" / "tt-project" / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "tt-project", "version": version, "skills": "./skills/"}))
    return root


def _cx_box(tmp_path, monkeypatch, clone_version="0.2.1", cached_version="0.2.1", others=()):
    """CODEX_HOME registering tenstorrent-skills at an installer clone serving `clone_version`, tt-project
    `cached_version` in its cache, a release folder at 0.3.0 and the `codex` stub first on PATH."""
    from ttp import codexmarket
    clone = _cx_market(tmp_path / "clone", clone_version, others)
    cur = tmp_path / "lib" / "0.3.0"
    (cur / ".codex-plugin").mkdir(parents=True)
    (cur / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "tt-project", "version": "0.3.0"}))
    codexmarket.write(cur)
    (tmp_path / "lib" / "current").symlink_to(cur)
    home = tmp_path / "codexhome"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(f'model = "m"\n\n[marketplaces.tenstorrent-skills]\nsource_type = "local"\n'
                                      f'source = "{clone}"\n\n[plugins."tt-project@tenstorrent-skills"]\nenabled = true\n'
                                      + "".join(f'\n[plugins."{o}@tenstorrent-skills"]\nenabled = true\n' for o in others))
    if cached_version:
        (home / "plugins" / "cache" / "tenstorrent-skills" / "tt-project" / cached_version).mkdir(parents=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "codex").write_text(f"#!{sys.executable}\n{CX_STUB}")
    (stubs / "codex").chmod(0o755)
    log = tmp_path / "codex.log"
    log.write_text("")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CX_LOG", str(log))
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    return home, tmp_path / "lib" / "current", clone, log


def _cx_calls(log):
    return log.read_text().splitlines()


def test_codex_cache_at_the_release_or_no_codex_marketplace_makes_no_call(env, tmp_path, monkeypatch):
    from ttp import codexmarket
    home, cur, clone, log = _cx_box(tmp_path, monkeypatch, cached_version="0.3.0")
    assert codexmarket.reconcile(cur, "0.3.0") == [] and _cx_calls(log) == []
    # Lagging, but no tt-project marketplace registered, the plugin removed, or no codex on PATH: nothing.
    (home / "plugins" / "cache" / "tenstorrent-skills" / "tt-project" / "0.3.0").rename(
        home / "plugins" / "cache" / "tenstorrent-skills" / "tt-project" / "0.2.1")
    cfg = (home / "config.toml").read_text()
    (home / "config.toml").write_text(cfg.replace("[marketplaces.tenstorrent-skills]", "[marketplaces.mine]"))
    assert codexmarket.reconcile(cur, "0.3.0") == []
    shutil.rmtree(home / "plugins")
    (home / "config.toml").write_text(cfg.replace("enabled = true", "enabled = false"))
    assert codexmarket.reconcile(cur, "0.3.0") == []
    (home / "config.toml").write_text(cfg)
    monkeypatch.setenv("PATH", "/nonexistent")
    assert codexmarket.reconcile(cur, "0.3.0") == [] and _cx_calls(log) == []


def test_codex_lagging_cache_behind_its_own_release_takes_one_plugin_add(env, tmp_path, monkeypatch):
    from ttp import codexmarket
    home, cur, clone, log = _cx_box(tmp_path, monkeypatch, clone_version="0.3.0")
    assert codexmarket.reconcile(cur, "0.3.0") == [
        "Codex: plugin cache refreshed to tt-project 0.3.0 (was 0.2.1); new sessions load it"]
    assert _cx_calls(log) == ["plugin add tt-project@tenstorrent-skills"]
    assert codexmarket.cached(home) == ["0.3.0"] and f'source = "{clone}"' in (home / "config.toml").read_text()
    assert codexmarket.reconcile(cur, "0.3.0") == [] and len(_cx_calls(log)) == 1   # idempotent
    # Enabled but never cached (removed by hand): added again.
    shutil.rmtree(home / "plugins")
    assert "(was none)" in codexmarket.reconcile(cur, "0.3.0")[0] and len(_cx_calls(log)) == 2


def test_codex_stale_installer_clone_moves_to_the_release_then_refreshes(env, tmp_path, monkeypatch):
    from ttp import codexmarket
    home, cur, clone, log = _cx_box(tmp_path, monkeypatch)
    out = codexmarket.reconcile(cur, "0.3.0")
    assert out == [f"Codex: the tenstorrent-skills marketplace served tt-project 0.2.1 from {clone}; now from {cur}",
                   "Codex: plugin cache refreshed to tt-project 0.3.0 (was 0.2.1); new sessions load it"]
    assert _cx_calls(log) == ["plugin marketplace remove tenstorrent-skills", f"plugin marketplace add {cur}",
                              "plugin add tt-project@tenstorrent-skills"]
    cfg = codexmarket.config(home)
    assert cfg["marketplaces"]["tenstorrent-skills"]["source"] == str(cur.resolve())
    assert cfg["plugins"]["tt-project@tenstorrent-skills"] == {"enabled": True} and cfg["model"] == "m"
    assert codexmarket.reconcile(cur, "0.3.0") == [] and len(_cx_calls(log)) == 3


def test_codex_marketplace_it_must_not_move_is_left_and_named(env, tmp_path, monkeypatch):
    from ttp import codexmarket
    home, cur, clone, log = _cx_box(tmp_path, monkeypatch, others=("tt-skills",))
    snap = (home / "config.toml").read_text()
    out = codexmarket.reconcile(cur, "0.3.0")
    assert len(out) == 1 and "also serves tt-skills@tenstorrent-skills, so it was left as is" in out[0]
    # A source type this does not know (git has its own test), or a release folder that is no Codex
    # marketplace (an older `ttp setup`): named too.
    (home / "config.toml").write_text(snap.replace("tt-skills", "x").replace('"local"', '"other"'))
    assert "is not a local folder" in codexmarket.reconcile(cur, "0.3.0")[0]
    (home / "config.toml").write_text(snap.replace('[plugins."tt-skills@tenstorrent-skills"]', "[x]"))
    (cur / ".agents" / "plugins" / "marketplace.json").unlink()
    assert "is no Codex marketplace of it: rerun `ttp setup`" in codexmarket.reconcile(cur, "0.3.0")[0]
    # A newer checkout the user registered (a developer's own tree) is theirs: never moved back.
    newer = _cx_market(tmp_path / "dev", "0.4.0")
    (home / "config.toml").write_text(snap.replace(str(clone), str(newer)).replace('[plugins."tt-skills@tenstorrent-skills"]', "[x]"))
    assert "serves the newer 0.4.0, so it was left as is" in codexmarket.reconcile(cur, "0.3.0")[0]
    assert _cx_calls(log) == [] and codexmarket.cached(home) == ["0.2.1"]


def test_codex_failed_or_hung_calls_are_lines_and_the_old_source_comes_back(env, tmp_path, monkeypatch):
    from ttp import codexmarket
    home, cur, clone, log = _cx_box(tmp_path, monkeypatch)
    monkeypatch.setenv("CX_FAIL", f"add {cur}")
    out = codexmarket.reconcile(cur, "0.3.0")
    assert out == [f"Codex: its tt-project cache holds 0.2.1, the release is 0.3.0; `codex plugin marketplace add "
                   f"{cur}` exited 3 (no network); {clone} is registered again"]
    assert codexmarket.config(home)["marketplaces"]["tenstorrent-skills"]["source"] == str(clone)
    monkeypatch.setenv("CX_FAIL", "plugin add")
    out = codexmarket.reconcile(cur, "0.3.0")
    assert "now from" in out[0] and "`codex plugin add tt-project@tenstorrent-skills` exited 3" in out[1]
    assert "the next `ttp setup` or `ttp doctor` retries it" in out[1] and codexmarket.cached(home) == ["0.2.1"]
    monkeypatch.delenv("CX_FAIL")
    monkeypatch.setenv("CX_HANG", "1")
    monkeypatch.setattr(codexmarket, "CALL_TIMEOUT_S", 0.5)
    t0 = time.time()
    out = codexmarket.reconcile(cur, "0.3.0")
    assert time.time() - t0 < 10 and "`codex plugin add tt-project@tenstorrent-skills` failed (" in out[0]


def test_codex_config_reads_without_a_toml_parser(env, tmp_path, monkeypatch):
    from ttp import codexmarket
    real = importlib.import_module

    def no_toml(name, *a):
        if name in ("tomllib", "tomli"):
            raise ImportError(name)
        return real(name, *a)
    (tmp_path / "config.toml").write_text(
        'model = "m"  # c\nplugins.\'a@b\'.enabled = false\n[marketplaces.tenstorrent-skills]\nsource_type = "local"\n'
        'source = "/x/\\"q\\""\n\n[plugins."tt-project@tenstorrent-skills"]\nenabled = true\n[projects."/p"]\n'
        'trust_level = "trusted"\n[[skills.config]]\npath = "/s"\n')
    parsed = codexmarket.config(tmp_path)
    assert parsed.pop("skills") == {"config": [{"path": "/s"}]}
    monkeypatch.setattr(importlib, "import_module", no_toml)
    assert codexmarket.config(tmp_path) == parsed == {
        "model": "m", "plugins": {"a@b": {"enabled": False}, "tt-project@tenstorrent-skills": {"enabled": True}},
        "marketplaces": {"tenstorrent-skills": {"source_type": "local", "source": '/x/"q"'}},
        "projects": {"/p": {"trust_level": "trusted"}}}


def test_ttp_setup_and_doctor_refresh_a_lagging_codex_cache_once(env, tmp_path, monkeypatch, capsys):
    from ttp import codexmarket
    home, _cur, clone, log = _cx_box(tmp_path, monkeypatch)
    version = json.loads((TTP.parents[1] / ".codex-plugin" / "plugin.json").read_text())["version"]

    def setup():
        r = subprocess.run([*TTP_NO_SYNC, "setup", "--bin-dir", str(tmp_path / "bin")], env=dict(os.environ),
                           capture_output=True, text=True, timeout=max(300, _load_scaled(300)))
        assert r.returncode == 0, r.stderr
        return r.stdout

    out = setup()
    lib = env["home"] / "lib" / version
    assert codexmarket.plugin_version(str(env["home"] / "lib" / "current")) == version
    assert f"Codex: plugin cache refreshed to tt-project {version} (was 0.2.1)" in out
    assert codexmarket.config(home)["marketplaces"]["tenstorrent-skills"]["source"] == str(lib.resolve())
    assert codexmarket.cached(home) == [version] and len(_cx_calls(log)) == 3
    assert "Codex:" not in setup() and len(_cx_calls(log)) == 3   # at the release: no codex call
    # `ttp doctor` says so, and fixes a lag itself.
    p = make(env)
    from ttp import cli
    cli.main(["doctor", p.name])
    assert f"codex plugin cache: tt-project {version}, as the installed release" in capsys.readouterr().out
    cache = home / "plugins" / "cache" / "tenstorrent-skills" / "tt-project"
    (cache / version).rename(cache / "0.2.1")
    cli.main(["doctor", p.name])
    assert f"Codex: plugin cache refreshed to tt-project {version} (was 0.2.1)" in capsys.readouterr().out
    assert _cx_calls(log)[3:] == ["plugin add tt-project@tenstorrent-skills"]


def test_codex_newer_cache_folder_an_older_registration_left_takes_one_add_not_one_per_run(env, tmp_path, monkeypatch):
    """A newer folder a developer's earlier registration left in the cache: `codex plugin add` drops it
    (measured on codex-cli 0.160 in a temp CODEX_HOME), so the second run makes no call."""
    from ttp import codexmarket
    home, cur, clone, log = _cx_box(tmp_path, monkeypatch, clone_version="0.3.0", cached_version="0.4.0")
    (home / "plugins" / "cache" / "tenstorrent-skills" / "tt-project" / "0.2.1").mkdir()
    assert codexmarket.reconcile(cur, "0.3.0") == [
        "Codex: plugin cache refreshed to tt-project 0.3.0 (was 0.4.0); new sessions load it"]
    assert codexmarket.cached(home) == ["0.3.0"]
    for _ in range(2):
        assert codexmarket.reconcile(cur, "0.3.0") == []
    assert _cx_calls(log) == ["plugin add tt-project@tenstorrent-skills"]


def test_codex_git_marketplace_is_upgraded_and_added_only_when_it_serves_the_release(env, tmp_path, monkeypatch):
    from ttp import codexmarket
    home, cur, clone, log = _cx_box(tmp_path, monkeypatch)
    repo = _cx_market(tmp_path / "repo", "0.2.9")
    (home / "config.toml").write_text((home / "config.toml").read_text().replace(
        f'source_type = "local"\nsource = "{clone}"', f'source_type = "git"\nsource = "{repo}"'))
    snap = (home / "config.toml").read_text()
    # Its branch is behind the release: refreshed, never moved, nothing added.
    out = codexmarket.reconcile(cur, "0.3.0")
    assert len(out) == 1 and f"is the git source {repo}, which serves 0.2.9, so it was left as is" in out[0]
    assert _cx_calls(log) == ["plugin marketplace upgrade tenstorrent-skills"] and codexmarket.cached(home) == ["0.2.1"]
    # Ahead of it (a newer push): left as is too.
    (repo / "plugins" / "tt-project" / ".codex-plugin" / "plugin.json").write_text(json.dumps({"version": "0.4.0"}))
    assert "which serves 0.4.0, so it was left as is" in codexmarket.reconcile(cur, "0.3.0")[0]
    # A failed upgrade (no network) is a line; the next run retries.
    monkeypatch.setenv("CX_FAIL", "upgrade")
    out = codexmarket.reconcile(cur, "0.3.0")
    assert "`codex plugin marketplace upgrade tenstorrent-skills` exited 3 (no network); the next" in out[0]
    monkeypatch.delenv("CX_FAIL")
    # Once it serves the release: one add, then nothing.
    (repo / "plugins" / "tt-project" / ".codex-plugin" / "plugin.json").write_text(json.dumps({"version": "0.3.0"}))
    assert codexmarket.reconcile(cur, "0.3.0") == [
        "Codex: plugin cache refreshed to tt-project 0.3.0 (was 0.2.1); new sessions load it"]
    assert codexmarket.reconcile(cur, "0.3.0") == [] and _cx_calls(log)[-2:] == [
        "plugin marketplace upgrade tenstorrent-skills", "plugin add tt-project@tenstorrent-skills"]
    assert len(_cx_calls(log)) == 5 and (home / "config.toml").read_text() == snap and codexmarket.cached(home) == ["0.3.0"]


def _cx_installed(monkeypatch, cur):
    """`cur` as the installed release, as once() finds it."""
    from ttp import release
    (cur / "runtime" / "ttp").mkdir(parents=True)
    (cur / "runtime" / "ttp" / "__init__.py").write_text('__version__ = "0.3.0"\n')
    monkeypatch.setattr(release, "installed", lambda: cur)


def test_codex_once_reconciles_each_release_once_bounded_and_never_raises(env, tmp_path, monkeypatch):
    from ttp import codexmarket
    home, cur, clone, log = _cx_box(tmp_path, monkeypatch, clone_version="0.3.0")
    _cx_installed(monkeypatch, cur)
    done = env["home"] / codexmarket.DONE
    assert codexmarket.once() == ["Codex: plugin cache refreshed to tt-project 0.3.0 (was 0.2.1); new sessions load it"]
    assert done.read_text() == "0.3.0\n"
    cache = home / "plugins" / "cache" / "tenstorrent-skills" / "tt-project"
    (cache / "0.3.0").rename(cache / "0.2.1")
    assert codexmarket.once() == [] and len(_cx_calls(log)) == 1   # this release had its turn
    # A new release (another stamp): its turn, bounded as a whole by once()'s timeout.
    done.write_text("0.2.1\n")
    monkeypatch.setenv("CX_HANG", "1")
    t0 = time.time()
    out = codexmarket.once(timeout_s=1)
    assert time.time() - t0 < 10 and codexmarket.CALL_TIMEOUT_S == 180
    assert "`codex plugin add tt-project@tenstorrent-skills` failed (" in out[0] and done.read_text() == "0.3.0\n"
    # Never raises; no Codex marketplace registered yet: no stamp, so a later start checks again.
    monkeypatch.delenv("CX_HANG")
    done.unlink()
    monkeypatch.setattr(codexmarket, "reconcile", lambda *a, **k: 1 / 0)
    assert codexmarket.once() == ["Codex: plugin cache check failed (ZeroDivisionError: division by zero)"]
    (home / "config.toml").write_text("")
    assert codexmarket.once() == [] and not done.exists()


def test_codex_once_runs_at_daemon_start_and_in_ttp_upgrade(env, monkeypatch):
    import inspect
    from ttp import cli, codexmarket, service
    from ttp.daemon import Daemon
    p = make(env)
    calls = []
    monkeypatch.setattr(codexmarket, "once", lambda: calls.append(1) or ["Codex: plugin cache refreshed (stub)"])
    Daemon(p.base)._codex_cache_once()
    assert "Codex: plugin cache refreshed (stub)" in (p.logs / "daemon.log").read_text()
    assert "target=self._codex_cache_once" in inspect.getsource(Daemon.run)   # off the start path, in a thread
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            cli.main(["upgrade", p.name])
        except SystemExit:
            pass
    assert calls == [1, 1] and "Codex: plugin cache refreshed (stub)" in out.getvalue()


def test_codex_ship_runtime_reaches_reconcile_on_the_remote_host(env, tmp_path, monkeypatch):
    """ship_runtime's `ttp setup` there makes the shipped release a Codex marketplace and refreshes
    that host's cache: run end to end through an `ssh` stand-in that runs the command in another HOME."""
    from ttp import cli, codexmarket
    home, _cur, clone, log = _cx_box(tmp_path, monkeypatch)
    far = tmp_path / "far"
    far.mkdir()
    (tmp_path / "stubs" / "ssh").write_text(
        f'#!/bin/sh\nfor a; do last=$a; done\nexec env -u TTP_HOME HOME={far} PYTHONDONTWRITEBYTECODE=1 sh -c "$last"\n')
    (tmp_path / "stubs" / "ssh").chmod(0o755)
    assert cli.ship_runtime("box") == f"~/.tt-project/lib/{cli.__version__}/bin/ttp"
    lib = far / ".tt-project" / "lib" / cli.__version__
    assert codexmarket.plugin_version(str(lib)) == cli.__version__ and (lib / ".claude-plugin" / "marketplace.json").is_file()
    assert codexmarket.cached(home) == [cli.__version__]
    assert codexmarket.config(home)["marketplaces"]["tenstorrent-skills"]["source"] == str(lib.resolve())
