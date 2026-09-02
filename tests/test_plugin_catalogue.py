"""Cross-host packaging and consent invariants for the plugin marketplace."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import yaml


REPO = pathlib.Path(__file__).resolve().parents[1]
PLUGINS = REPO / "plugins"
CODEX_MARKETPLACE = REPO / ".agents" / "plugins" / "marketplace.json"
CLAUDE_MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_host_catalogues_expose_the_same_plugins():
    codex = load(CODEX_MARKETPLACE)
    claude = load(CLAUDE_MARKETPLACE)
    codex_entries = {entry["name"]: entry for entry in codex["plugins"]}
    claude_entries = {entry["name"]: entry for entry in claude["plugins"]}
    assert codex_entries.keys() == claude_entries.keys()

    for name, entry in codex_entries.items():
        expected = f"./plugins/{name}"
        assert entry["source"] == {"source": "local", "path": expected}
        assert claude_entries[name]["source"] == expected


def test_only_finder_is_installed_by_default():
    entries = {entry["name"]: entry for entry in load(CODEX_MARKETPLACE)["plugins"]}
    assert entries["tt-skills"]["policy"]["installation"] == "INSTALLED_BY_DEFAULT"
    for name, entry in entries.items():
        if name != "tt-skills":
            assert entry["policy"]["installation"] == "AVAILABLE"
        assert entry["policy"]["authentication"] in {"ON_INSTALL", "ON_USE"}
        assert entry["category"]


def test_each_plugin_has_matching_host_manifests():
    names = {entry["name"] for entry in load(CODEX_MARKETPLACE)["plugins"]}
    for name in names:
        root = PLUGINS / name
        codex = load(root / ".codex-plugin" / "plugin.json")
        claude = load(root / ".claude-plugin" / "plugin.json")
        assert codex["name"] == claude["name"] == name
        assert codex["version"] == claude["version"]
        assert codex["skills"] == claude["skills"] == "./skills/"
        assert (root / "skills").is_dir()


def test_codex_manifests_have_complete_install_surfaces():
    required_interface = {
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "defaultPrompt",
    }
    for manifest_path in PLUGINS.glob("*/.codex-plugin/plugin.json"):
        manifest = load(manifest_path)
        assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
        assert manifest["description"]
        assert manifest["author"]["name"]
        assert manifest["license"]
        assert required_interface <= manifest["interface"].keys()
        assert manifest["interface"]["capabilities"]
        assert manifest["interface"]["defaultPrompt"]


def test_packaged_skills_have_discoverable_frontmatter():
    for path in PLUGINS.glob("*/skills/*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        end = text.find("\n---", 3)
        assert end != -1
        frontmatter = yaml.safe_load(text[3:end]) or {}
        assert frontmatter.get("name") == path.parent.name
        assert frontmatter.get("description")


def test_plugins_are_individually_owned():
    codeowners = (REPO / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    for plugin in PLUGINS.iterdir():
        if plugin.is_dir():
            assert f"/plugins/{plugin.name}/" in codeowners


def test_finder_catalogues_every_optional_plugin():
    finder = (
        PLUGINS
        / "tt-skills"
        / "skills"
        / "tt-skills-finder"
        / "references"
        / "catalog.md"
    ).read_text(encoding="utf-8")
    entries = load(CODEX_MARKETPLACE)["plugins"]
    optional = {
        entry["name"]
        for entry in entries
        if entry["policy"]["installation"] != "INSTALLED_BY_DEFAULT"
    }
    assert optional
    for name in optional:
        assert f"`{name}`" in finder


def test_finder_is_discovery_only():
    skill = (
        PLUGINS / "tt-skills" / "skills" / "tt-skills-finder" / "SKILL.md"
    ).read_text(encoding="utf-8").lower()
    assert "a recommendation is not permission" in skill
    assert "do not run an installation command" in skill
    assert "explicitly asks to install" in skill


def test_packaged_review_skills_are_current():
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "sync_review_plugin.py"), "--check"],
        cwd=REPO,
        check=True,
    )
