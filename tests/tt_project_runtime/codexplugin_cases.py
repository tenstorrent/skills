# Isolated Codex runs keep tt-project's own installed skills (providers/codex.py own_plugin_args).
from __future__ import annotations

import importlib


def _codex_install(home, market="tenstorrent-skills", version="1.0.0", manifest=None, extra=()):
    """A copy of tt-project in Codex's install cache, as `codex plugin add` leaves it."""
    root = home / "plugins" / "cache" / market / "tt-project" / version
    (root / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "tt-project-harness").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "tt-project-harness" / "SKILL.md").write_text("---\nname: tt-project-harness\n---\n")
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(manifest if manifest is not None else {"name": "tt-project", "skills": "./skills/"}))
    for name in extra:
        (root / name).mkdir() if name == "hooks" else (root / name).write_text("{}")
    return root


def _codex_plugin_table(argv):
    """The plugins a Codex argv enables by `-c plugins={...}`, read with a TOML parser where one loads
    (else the one inline-table shape the adapter writes); None: no key."""
    keys = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c" and argv[i + 1].startswith("plugins")]
    assert len(keys) <= 1, keys
    if not keys:
        return None
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name).loads(keys[0])["plugins"]
        except ImportError:
            continue
    m = re.fullmatch(r'plugins=\{("(?:[^"\\]|\\.)*")=\{enabled=true\}\}', keys[0])
    assert m, keys[0]
    return {json.loads(m.group(1)): {"enabled": True}}


def test_codex_isolated_worker_keeps_the_installed_tt_project_skills(env, monkeypatch, tmp_path):
    from ttp.providers import get_provider
    worker, home, _ = _codex_hooks(monkeypatch, tmp_path)
    (home / "config.toml").write_text('[plugins."tt-project@tenstorrent-skills"]\nenabled = true\n')
    assert _codex_plugin_table(worker()[0].isolation_args()) is None, "nothing installed: nothing to enable"
    _codex_install(home)
    prov, argv = worker()
    iso = prov.isolation_args()
    assert iso[0] == "--ignore-user-config"
    assert _codex_plugin_table(iso) == {"tt-project@tenstorrent-skills": {"enabled": True}}
    # The config's own plugins held the hook back; the isolated run gets it back, the plugin enabled.
    assert "--dangerously-bypass-hook-trust" in iso and set(_codex_hook_commands(iso + ["-"])) == {"PreToolUse",
                                                                                                    "PostToolUse"}
    # A run that reads config.toml loads the plugin from there: a whole table would replace the user's.
    assert _codex_plugin_table(argv) is None
    coord = get_provider("codex").build(role="coordinator", model="", effort="", cwd=str(tmp_path), budget_usd=None,
                                        read_only=True, schema=None, restrictions={})[0]
    assert "--ignore-user-config" in coord and _codex_plugin_table(coord) is None, "decision-only turns: no skills"
    # No --ignore-user-config (a config with the run's model provider, or an older build): no table either.
    (home / "config.toml").write_text('model_provider = "local"\n[plugins."tt-project@tenstorrent-skills"]\n')
    assert worker()[0].isolation_args() == []
    _codex_hooks(monkeypatch, tmp_path, CODEX_EXEC_HELP)
    (home / "config.toml").write_text('model = "m"\n')
    assert worker()[0].isolation_args() == []
    # Claude's isolation is unchanged.
    assert get_provider("claude").isolation_args() == ["--strict-mcp-config", "--setting-sources", "project,local"]


@pytest.mark.parametrize("bad", [{"extra": ["hooks"]}, {"extra": ["hooks.json"]}, {"extra": [".mcp.json"]},
                                 {"extra": [".app.json"]},
                                 {"manifest": {"name": "tt-project", "hooks": "./hooks/hooks.json"}},
                                 {"manifest": {"name": "tt-project", "mcpServers": "./.mcp.json"}},
                                 {"manifest": {"name": "tt-project", "apps": "./.app.json"}},
                                 {"manifest": ["not", "an", "object"]}])
def test_codex_isolation_enables_no_copy_that_brings_more_than_skills(env, monkeypatch, tmp_path, bad):
    """Hooks, MCP servers or apps would undo the isolation (and the hook trust bypass would trust the
    hooks): such a copy, or any version of it, stays off. So do a broken or missing manifest."""
    worker, home, _ = _codex_hooks(monkeypatch, tmp_path)
    _codex_install(home, version="1.0.0")
    _codex_install(home, version="2.0.0", **bad)
    assert worker()[0].isolation_args() == ["--ignore-user-config"], bad


def test_codex_isolation_skips_an_unreadable_or_empty_install(env, monkeypatch, tmp_path):
    worker, home, _ = _codex_hooks(monkeypatch, tmp_path)
    root = _codex_install(home)
    (root / ".codex-plugin" / "plugin.json").write_text("{not json")
    assert _codex_plugin_table(worker()[0].isolation_args()) is None, "a manifest that does not parse"
    (root / ".codex-plugin" / "plugin.json").unlink()
    assert _codex_plugin_table(worker()[0].isolation_args()) is None, "no manifest"
    import shutil
    shutil.rmtree(root)
    assert _codex_plugin_table(worker()[0].isolation_args()) is None, "no version left"
    (home / "plugins" / "cache" / "m2").mkdir()
    (home / "plugins" / "cache" / "m2" / "tt-project").write_text("a file, not a plugin")
    assert _codex_plugin_table(worker()[0].isolation_args()) is None


def test_codex_isolation_enables_one_marketplace_preferring_the_configs(env, monkeypatch, tmp_path):
    worker, home, _ = _codex_hooks(monkeypatch, tmp_path)
    _codex_install(home, market="b-market")
    _codex_install(home, market='a"market')   # a name TOML must quote
    assert _codex_plugin_table(worker()[0].isolation_args()) == {'tt-project@a"market': {"enabled": True}}
    for line in ('[plugins."tt-project@b-market"]\nenabled = true\n', "plugins.'tt-project@b-market'.enabled = true\n"):
        (home / "config.toml").write_text('model = "m"\n' + line)
        assert _codex_plugin_table(worker()[0].isolation_args()) == {"tt-project@b-market": {"enabled": True}}, line
    # Another plugin of the same marketplace is never enabled with it.
    _codex_install(home, market="b-market").parent.parent.joinpath("lint").mkdir()
    assert list(_codex_plugin_table(worker()[0].isolation_args())) == ["tt-project@b-market"]
