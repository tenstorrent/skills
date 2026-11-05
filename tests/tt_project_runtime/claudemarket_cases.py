# `ttp setup`'s Claude Code marketplace step (claudemarket.py) tests of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there). Add new ones here, not there.
from __future__ import annotations


def _cm_skills_clone(root, version):
    """A skills checkout as the installer clones it: a marketplace with tt-project at `version`."""
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "tenstorrent-skills", "plugins": [
        {"name": "tt-buddy", "source": "./plugins/tt-buddy"}, {"name": "tt-project", "source": "./plugins/tt-project"}]}))
    (root / "plugins" / "tt-project" / ".claude-plugin").mkdir(parents=True)
    (root / "plugins" / "tt-project" / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    return root


def _cm_box(tmp_path, stale="0.2.1", others=()):
    """A Claude config dir registering tenstorrent-skills at an installer clone holding tt-project `stale`
    beside unrelated marketplaces, an installed release folder, and a `claude` stub logging its calls."""
    from ttp import claudemarket
    clone = _cm_skills_clone(tmp_path / "clone", stale)
    cur = tmp_path / "lib" / "current"
    (cur / ".claude-plugin").mkdir(parents=True)
    (cur / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "tt-project", "version": "0.3.0"}))
    claudemarket.write(cur)
    cfg = tmp_path / "claude"
    (cfg / "plugins").mkdir(parents=True)
    src = {"source": "directory", "path": str(clone)}
    other = {"source": {"source": "directory", "path": str(tmp_path / "mine")}}
    gh = {"source": {"source": "github", "repo": "example/market"}}
    (cfg / "settings.json").write_text(json.dumps({
        "model": "x", "extraKnownMarketplaces": {"tenstorrent-skills": {"source": src}, "mine": other, "gh": gh},
        "enabledPlugins": {"tt-project@tenstorrent-skills": True, "a@mine": True,
                           **{f"{o}@tenstorrent-skills": True for o in others}}}, indent=2))
    (cfg / "plugins" / "known_marketplaces.json").write_text(json.dumps({
        "tenstorrent-skills": {"source": src, "installLocation": str(clone), "lastUpdated": "t"},
        "mine": {**other, "installLocation": str(tmp_path / "mine")}}, indent=2))
    (cfg / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "tt-project@tenstorrent-skills": [{"scope": "user", "version": stale}]}}))
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "claude").write_text('#!/bin/sh\necho "$*" >> "$CM_LOG"\n')
    (stubs / "claude").chmod(0o755)
    return cfg, cur, clone, stubs


def _cm_files(cfg):
    return {p.relative_to(cfg).as_posix(): p.read_bytes() for p in sorted(cfg.rglob("*")) if p.is_file()}


def test_setup_repoints_a_stale_tt_project_marketplace_once_and_leaves_others(tmp_path, monkeypatch):
    from ttp import claudemarket
    cfg, cur, clone, stubs = _cm_box(tmp_path)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("CM_LOG", str(log))
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    before = json.loads((cfg / "settings.json").read_text())
    known_before = json.loads((cfg / "plugins" / "known_marketplaces.json").read_text())
    out = "\n".join(claudemarket.reconcile(cur, "0.3.0", cfg))
    s = json.loads((cfg / "settings.json").read_text())
    k = json.loads((cfg / "plugins" / "known_marketplaces.json").read_text())
    # (1) no stale tt-project path registered anywhere
    assert s["extraKnownMarketplaces"]["tenstorrent-skills"]["source"] == {"source": "directory", "path": str(cur)}
    assert k["tenstorrent-skills"]["source"]["path"] == k["tenstorrent-skills"]["installLocation"] == str(cur)
    assert str(clone) not in (cfg / "settings.json").read_text() + (cfg / "plugins" / "known_marketplaces.json").read_text()
    assert claudemarket.plugin_version(str(cur)) == "0.3.0"
    # (2) unrelated entries and keys are left as they were
    assert {n: e for n, e in s["extraKnownMarketplaces"].items() if n != "tenstorrent-skills"} == \
        {n: e for n, e in before["extraKnownMarketplaces"].items() if n != "tenstorrent-skills"}
    assert s["model"] == "x" and s["enabledPlugins"] == before["enabledPlugins"]
    assert k["mine"] == known_before["mine"] and k["tenstorrent-skills"]["lastUpdated"] == "t"
    # each edited file was backed up first, and the plugin cache refreshed
    backups = sorted(p.name.split(".ttp-backup-")[0] for p in cfg.rglob("*.ttp-backup-*"))
    assert backups == ["known_marketplaces.json", "settings.json"]
    assert json.loads(next(cfg.glob("settings.json.ttp-backup-*")).read_text()) == before
    assert log.read_text().splitlines() == ["plugin marketplace update tenstorrent-skills",
                                            "plugin update tt-project@tenstorrent-skills"]
    assert "now at" in out and "plugin cache refreshed to tt-project 0.3.0 (was 0.2.1)" in out
    # (3) a second run changes nothing once Claude Code cached the release
    inst = cfg / "plugins" / "installed_plugins.json"
    inst.write_text(inst.read_text().replace('"0.2.1"', '"0.3.0"'))
    snap = _cm_files(cfg)
    assert claudemarket.reconcile(cur, "0.3.0", cfg) == []
    assert _cm_files(cfg) == snap and len(log.read_text().splitlines()) == 2


def test_setup_only_refreshes_a_stale_plugin_cache_behind_its_own_marketplace(tmp_path, monkeypatch):
    from ttp import claudemarket
    cfg, cur, clone, stubs = _cm_box(tmp_path)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("CM_LOG", str(log))
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    for f in (cfg / "settings.json", cfg / "plugins" / "known_marketplaces.json"):
        f.write_text(f.read_text().replace(json.dumps(str(clone))[1:-1], str(cur)))
    snap = _cm_files(cfg)
    assert "refreshed" in "\n".join(claudemarket.reconcile(cur, "0.3.0", cfg))
    assert _cm_files(cfg) == snap and len(log.read_text().splitlines()) == 2
    # The same stale cache behind a marketplace from elsewhere (a pinned git ref): not pulled on every setup.
    log.unlink()
    for f in (cfg / "settings.json", cfg / "plugins" / "known_marketplaces.json"):
        f.write_text(f.read_text().replace(str(cur), str(tmp_path / "elsewhere")))
    assert claudemarket.reconcile(cur, "0.3.0", cfg) == [] and not log.exists()


def test_setup_leaves_marketplaces_it_must_not_repoint(tmp_path, monkeypatch):
    from ttp import claudemarket
    monkeypatch.setenv("CM_LOG", str(tmp_path / "claude.log"))
    # Another plugin of the same marketplace is enabled: repointing would drop it, so only a line.
    cfg, cur, clone, stubs = _cm_box(tmp_path / "a", others=("tt-buddy",))
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    snap = _cm_files(cfg)
    out = claudemarket.reconcile(cur, "0.3.0", cfg)
    assert len(out) == 1 and "also serves tt-buddy@tenstorrent-skills" in out[0] and "left as is" in out[0]
    assert _cm_files(cfg) == snap
    # The registered copy is as new as the release, or newer: nothing to do.
    for v in ("0.3.0", "0.4.0"):
        cfg, cur, clone, stubs = _cm_box(tmp_path / v, stale=v)
        snap = _cm_files(cfg)
        assert claudemarket.reconcile(cur, "0.3.0", cfg) == [] and _cm_files(cfg) == snap
    # A tenstorrent-skills directory that holds no tt-project, a broken settings file, no Claude Code at all.
    cfg, cur, clone, stubs = _cm_box(tmp_path / "b")
    (clone / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"name": "x", "plugins": [
        {"name": "tt-buddy", "source": "./plugins/tt-buddy"}]}))
    (cfg / "plugins" / "installed_plugins.json").unlink()
    snap = _cm_files(cfg)
    assert claudemarket.reconcile(cur, "0.3.0", cfg) == [] and _cm_files(cfg) == snap
    cfg, cur, clone, stubs = _cm_box(tmp_path / "c")
    (cfg / "settings.json").write_text("{not json")
    (cfg / "plugins" / "known_marketplaces.json").write_text("[]")
    (cfg / "plugins" / "installed_plugins.json").unlink()
    snap = _cm_files(cfg)
    assert claudemarket.reconcile(cur, "0.3.0", cfg) == [] and _cm_files(cfg) == snap
    assert claudemarket.reconcile(cur, "0.3.0", tmp_path / "no-claude") == []


def test_setup_reports_a_settings_file_it_cannot_write_and_a_missing_claude(tmp_path, monkeypatch):
    from ttp import claudemarket
    cfg, cur, clone, stubs = _cm_box(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))      # no `claude` CLI
    real = claudemarket._backup

    def denied(f):
        if f.name == "settings.json":
            raise PermissionError(13, "Permission denied")
        return real(f)
    monkeypatch.setattr(claudemarket, "_backup", denied)
    snap = _cm_files(cfg)
    out = claudemarket.reconcile(cur, "0.3.0", cfg)
    assert len(out) == 1 and "could not repoint" in out[0] and _cm_files(cfg) == snap
    monkeypatch.setattr(claudemarket, "_backup", real)
    out = "\n".join(claudemarket.reconcile(cur, "0.3.0", cfg))
    assert "now at" in out and "run `claude plugin marketplace update tenstorrent-skills && " \
        "claude plugin update tt-project@tenstorrent-skills`" in out


def test_ttp_setup_repoints_a_stale_installer_clone_to_lib_current(env, tmp_path):
    from ttp import claudemarket
    cfg, _cur, clone, stubs = _cm_box(tmp_path)
    log = tmp_path / "claude.log"
    run_env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg), "CM_LOG": str(log),
               "PATH": f"{stubs}:{os.environ['PATH']}"}

    def setup():
        r = subprocess.run([*TTP_NO_SYNC, "setup", "--bin-dir", str(tmp_path / "bin")], env=run_env,
                           capture_output=True, text=True, timeout=max(300, _load_scaled(300)))
        assert r.returncode == 0, r.stderr
        return r.stdout

    out = setup()
    cur = env["home"] / "lib" / "current"
    version = json.loads((TTP.parents[1] / ".claude-plugin" / "plugin.json").read_text())["version"]
    assert claudemarket.plugin_version(str(cur)) == version
    s = json.loads((cfg / "settings.json").read_text())
    assert s["extraKnownMarketplaces"]["tenstorrent-skills"]["source"]["path"] == str(cur)
    assert "now at" in out and len(log.read_text().splitlines()) == 2
    inst = cfg / "plugins" / "installed_plugins.json"
    inst.write_text(inst.read_text().replace('"0.2.1"', json.dumps(version)))
    snap = _cm_files(cfg)
    assert "Claude Code: " not in setup().replace("Claude Code: /tt-project is ready", "")
    assert _cm_files(cfg) == snap and len(log.read_text().splitlines()) == 2


def test_setup_never_installs_tt_project_into_claude_code_on_its_own(tmp_path, monkeypatch):
    from ttp import claudemarket
    cfg, cur, clone, stubs = _cm_box(tmp_path)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("CM_LOG", str(log))
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
    (cfg / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": []}))
    out = "\n".join(claudemarket.reconcile(cur, "0.3.0", cfg))
    assert "marketplace refreshed to tt-project 0.3.0" in out
    assert log.read_text().splitlines() == ["plugin marketplace update tenstorrent-skills"]
    # A `claude` that fails or hangs is named, and the next setup retries it.
    (stubs / "claude").write_text('#!/bin/sh\necho "no network" >&2\nexit 3\n')
    inst = cfg / "plugins" / "installed_plugins.json"
    inst.write_text(json.dumps({"version": 2, "plugins": {"tt-project@tenstorrent-skills": [{"version": "0.2.1"}]}}))
    out = claudemarket.reconcile(cur, "0.3.0", cfg)
    assert out == ["Claude Code: `claude plugin marketplace update tenstorrent-skills` exited 3 (no network); "
                   "the next `ttp setup` retries it"]
    monkeypatch.setattr(claudemarket, "REFRESH_TIMEOUT_S", 0.5)
    (stubs / "claude").write_text('#!/bin/sh\nexec sleep 5\n')
    assert "failed" in claudemarket.reconcile(cur, "0.3.0", cfg)[0]
